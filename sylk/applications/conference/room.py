# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import hashlib
import os
import random
import re
import shutil

from datetime import datetime
from glob import glob
from itertools import cycle
from time import mktime

try:
    from weakref import WeakSet
except ImportError:
    from sylk.thirdparty.weakrefset import WeakSet

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from eventlet import api, coros, proc
from itertools import chain
from sipsimple.account import AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPCoreInvalidStateError, SIPURI
from sipsimple.core import Header, ContactHeader, FromHeader, ToHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import conference
from sipsimple.streams import FileTransferStream
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.streams.msrp import ChatStreamError, FileSelector
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread, run_in_waitable_green_thread
from sipsimple.util import Timestamp, TimestampedNotificationData, makedirs
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications.conference import database
from sylk.applications.conference.configuration import ConferenceConfig
from sylk.configuration import SIPConfig, ThorNodeConfig
from sylk.configuration.datatypes import ResourcePath
from sylk.session import ServerSession


def format_identity(identity, cpim_format=False):
    uri = identity.uri
    if identity.display_name:
        return u'%s <sip:%s@%s>' % (identity.display_name, uri.user, uri.host)
    elif cpim_format:
        return u'<sip:%s@%s>' % (uri.user, uri.host)
    else:
        return u'sip:%s@%s' % (uri.user, uri.host)


class Room(object):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """
    implements(IObserver)

    def __init__(self, uri):
        self._channel = coros.queue()
        self.uri = uri
        self.identity = CPIMIdentity.parse('<sip:%s>' % self.uri)
        self.files = []
        self.sessions = []
        self.sessions_with_proposals = []
        self.subscriptions = []
        self.transfer_handlers = WeakSet()
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.moh_player = None
        self.conference_info_payload = None

    @property
    def empty(self):
        return len(self.sessions) == 0

    @property
    def started(self):
        return self.state == 'started'

    @property
    def stopping(self):
        return self.state in ('stopping', 'stopped')

    @property
    def active_media(self):
        return set((stream.type for stream in chain(*(session.streams for session in self.sessions if session.streams))))

    def start(self):
        if self.started:
            return
        self.message_dispatcher = proc.spawn(self._message_dispatcher)
        self.audio_conference = AudioConference()
        self.audio_conference.hold()
        self.moh_player = MoHPlayer(self.audio_conference)
        self.moh_player.initialize()
        self.state = 'started'

    @run_in_waitable_green_thread
    def stop(self):
        if not self.started:
            return
        self.state = 'stopping'
        self.incoming_message_queue.send_exception(api.GreenletExit)
        self.incoming_message_queue = None
        self.message_dispatcher.kill(proc.ProcExit)
        self.message_dispatcher = None
        self.moh_player.stop()
        self.moh_player = None
        self.audio_conference = None
        procs = [proc.spawn(handler.stop) for handler in self.transfer_handlers]
        proc.waitall(procs)
        [subscription.end() for subscription in self.subscriptions]
        wait_count = len(self.subscriptions)
        while wait_count > 0:
            notification = self._channel.wait()
            if notification.name == 'SIPIncomingSubscriptionDidEnd':
                wait_count -= 1
        self.subscriptions = []
        self.cleanup_files()
        self.conference_info_payload = None
        self.state = 'stopped'

    @run_in_thread('file-io')
    def cleanup_files(self):
        path = os.path.join(ConferenceConfig.file_transfer_dir, self.uri)
        try:
            shutil.rmtree(path)
        except EnvironmentError:
            pass

    def _message_dispatcher(self):
        """Read from self.incoming_message_queue and dispatch the messages to other participants"""
        while True:
            session, message_type, data = self.incoming_message_queue.wait()
            if message_type == 'message':
                message = data.message
                if message.sender.uri != session.remote_identity.uri:
                    return
                if data.timestamp is not None and isinstance(message.timestamp, Timestamp):
                    timestamp = datetime.fromtimestamp(mktime(message.timestamp.timetuple()))
                else:
                    timestamp = datetime.now()
                database.async_save_message(format_identity(session.remote_identity, True), self.uri, message.body, message.content_type, unicode(message.sender), unicode(message.recipients[0]), timestamp)
                if data.private:
                    self.dispatch_private_message(session, message)
                else:
                    self.dispatch_message(session, message)
            elif message_type == 'composing_indication':
                if data.sender.uri != session.remote_identity.uri:
                    return
                if data.private:
                    self.dispatch_private_iscomposing(session, data)
                else:
                    self.dispatch_iscomposing(session, data)

    def dispatch_message(self, session, message):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                try:
                    chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[self.identity], timestamp=message.timestamp)
                except ChatStreamError, e:
                    log.error(u'Error dispatching message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_message(self, session, message):
        # Private messages are delivered to all sessions matching the recipient but also to the sender,
        # for replication in clients
        recipient = message.recipients[0]
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri in (recipient.uri, session.remote_identity.uri)):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                continue
            else:
                try:
                    chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[recipient], timestamp=message.timestamp)
                except ChatStreamError, e:
                    log.error(u'Error dispatching private message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_iscomposing(self, session, data):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                try:
                    chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity, recipients=[self.identity])
                except ChatStreamError, e:
                    log.error(u'Error dispatching composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_iscomposing(self, session, data):
        recipient_uri = data.recipients[0].uri
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri == recipient_uri):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                continue
            else:
                try:
                    chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity)
                except ChatStreamError, e:
                    log.error(u'Error dispatching private composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])
        self_identity = format_identity(self.identity, cpim_format=True)
        database.async_save_message(self_identity, self.uri, body, content_type, self_identity, self_identity, datetime.now())

    def dispatch_conference_info(self):
        data = self.build_conference_info_payload()
        for subscription in (subscription for subscription in self.subscriptions if subscription.state == 'active'):
            try:
                subscription.push_content(conference.Conference.content_type, data)
            except (SIPCoreError, SIPCoreInvalidStateError):
                pass

    def dispatch_file(self, file):
        self.dispatch_server_message('%s has uploaded file %s (%s)' % (file.sender, os.path.basename(file.name), self.format_file_size(file.size)))
        sender_uri = CPIMIdentity.parse(file.sender).uri
        for uri in set(session.remote_identity.uri for session in self.sessions if str(session.remote_identity.uri) != str(sender_uri)):
            handler = OutgoingFileTransferHandler(self, uri, file)
            self.transfer_handlers.add(handler)
            handler.start()

    def add_session(self, session):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        self.sessions.append(session)
        try:
            chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=chat_stream)
        try:
            audio_stream = (stream for stream in session.streams if stream.type == 'audio').next()
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=audio_stream)
            log.msg(u'Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                      'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
        try:
            transfer_stream = (stream for stream in session.streams if stream.type == 'file-transfer').next()
        except StopIteration:
            pass
        else:
            if transfer_stream.direction == 'recvonly':
                transfer_handler = IncomingFileTransferHandler(self, session)
                transfer_handler.start()
                txt = u'%s is uploading file %s' % (format_identity(session.remote_identity, cpim_format=True), transfer_stream.file_selector.name.decode('utf-8'))
            else:
                transfer_handler = OutgoingFileTransferRequestHandler(self, session)
                transfer_handler.start()
                txt = u'%s requested file %s' % (format_identity(session.remote_identity, cpim_format=True), transfer_stream.file_selector.name.decode('utf-8'))
            log.msg(txt)
            self.dispatch_server_message(txt)
            if len(session.streams) == 1:
                return

        welcome_handler = WelcomeHandler(self, session)
        welcome_handler.start()
        self.dispatch_conference_info()

        if len(self.sessions) == 1:
            log.msg(u'%s started conference %s %s' % (format_identity(session.remote_identity), self.uri, self.format_stream_types(session.streams)))
        else:
            log.msg(u'%s joined conference %s %s' % (format_identity(session.remote_identity), self.uri, self.format_stream_types(session.streams)))
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has joined the room %s' % (format_identity(session.remote_identity), self.format_stream_types(session.streams)), exclude=session)

    def remove_session(self, session):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        self.sessions.remove(session)
        try:
            chat_stream = (stream for stream in session.streams or [] if stream.type == 'chat').next()
        except StopIteration:
            pass
        else:
            notification_center.remove_observer(self, sender=chat_stream)
        try:
            audio_stream = (stream for stream in session.streams or [] if stream.type == 'audio').next()
        except StopIteration:
            pass
        else:
            notification_center.remove_observer(self, sender=audio_stream)
            try:
                self.audio_conference.remove(audio_stream)
            except ValueError:
                # User may hangup before getting bridged into the conference
                pass
            if len(self.audio_conference.streams) == 0:
                self.moh_player.pause()
                self.audio_conference.hold()
            elif len(self.audio_conference.streams) == 1:
                self.moh_player.play()
        try:
            transfer_stream = (stream for stream in session.streams if stream.type == 'file-transfer').next()
        except StopIteration:
            pass
        else:
            if len(session.streams) == 1:
                return

        self.dispatch_conference_info()
        log.msg(u'%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, self.format_session_duration(session)))
        if not self.sessions:
            log.msg(u'Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), self.format_session_duration(session)))

    def terminate_sessions(self, uri):
        if not self.started:
            return
        for session in (session for session in self.sessions if session.remote_identity.uri == uri):
            session.end()

    def build_conference_info_payload(self):
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = conference.ConferenceDescription(display_text='Ad-hoc conference', free_text='Hosted by %s' % settings.user_agent)
            host_info = conference.HostInfo(web_page=conference.WebPage('http://sylkserver.com'))
            self.conference_info_payload = conference.Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=conference.Users())
        user_count = len(set(str(s.remote_identity.uri) for s in self.sessions))
        self.conference_info_payload.conference_state = conference.ConferenceState(user_count=user_count, active=True)
        users = conference.Users()
        for session in (session for session in self.sessions if not (len(session.streams) == 1 and session.streams[0].type == 'file-transfer')):
            try:
                user = (user for user in users if user.entity == str(session.remote_identity.uri)).next()
            except StopIteration:
                user = conference.User(str(session.remote_identity.uri), display_text=session.remote_identity.display_name)
                users.append(user)
            joining_info = conference.JoiningInfo(when=session.start_time)
            holdable_streams = [stream for stream in session.streams if stream.hold_supported]
            session_on_hold = holdable_streams and all(stream.on_hold_by_remote for stream in holdable_streams)
            hold_status = conference.EndpointStatus('on-hold' if session_on_hold else 'connected')
            endpoint = conference.Endpoint(str(session._invitation.remote_contact_header.uri), display_text=session.remote_identity.display_name, joining_info=joining_info, status=hold_status)
            for stream in session.streams:
                if stream.type == 'file-transfer':
                    continue
                endpoint.append(conference.Media(id(stream), media_type=self.format_conference_stream_type(stream)))
            user.append(endpoint)
        self.conference_info_payload.users = users
        if self.files:
            conference_description = self.conference_info_payload.conference_description
            conference_description.resources = conference.Resources(conference.FileResources())
            for file in self.files:
                conference_description.resources.files.append(conference.FileResource(os.path.basename(file.name), file.hash, file.size, file.sender, file.status))
        return self.conference_info_payload.toxml()

    def handle_incoming_subscription(self, subscribe_request, data):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=subscribe_request)
        data = self.build_conference_info_payload()
        subscribe_request.accept(conference.Conference.content_type, data)
        self.subscriptions.append(subscribe_request)

    def accept_proposal(self, session, streams):
        if session in self.sessions_with_proposals:
            session.accept_proposal(streams)
            self.sessions_with_proposals.remove(session)

    def add_file(self, file):
        self.files.append(file)
        self.dispatch_conference_info()
        self.dispatch_file(file)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AudioStreamDidTimeout(self, notification):
        stream = notification.sender
        session = stream._session
        log.msg(u'Audio stream for session %s timed out' % format_identity(session.remote_identity))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        data = notification.data
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'message', data))

    def _NH_ChatStreamGotComposingIndication(self, notification):
        data = notification.data
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'composing_indication', data))

    def _NH_SIPIncomingSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=subscription)
        if self.state == 'stopping':
            self._channel.send(notification)
        else:
            self.subscriptions.remove(subscription)

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        session = notification.sender
        if notification.data.originator == 'remote':
            if notification.data.on_hold:
                log.msg(u'%s has put the audio session on hold' % format_identity(session.remote_identity))
            else:
                log.msg(u'%s has taken the audio session out of hold' % format_identity(session.remote_identity))
            self.dispatch_conference_info()

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            session.reject_proposal()
            return
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        self.sessions_with_proposals.append(session)
        reactor.callLater(4, self.accept_proposal, session, streams)

    def _NH_SIPSessionGotRejectProposal(self, notification):
        session = notification.sender
        self.sessions_with_proposals.remove(session)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        notification_center = NotificationCenter()
        session = notification.sender
        streams = notification.data.streams
        if notification.data.action == 'add':
            try:
                chat_stream = (stream for stream in streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                notification_center.add_observer(self, sender=chat_stream)
                log.msg(u'%s has added chat to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added chat' % format_identity(session.remote_identity), exclude=session)
            try:
                audio_stream = (stream for stream in streams if stream.type == 'audio').next()
            except StopIteration:
                pass
            else:
                notification_center.add_observer(self, sender=audio_stream)
                log.msg(u'Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                          'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                          audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                          audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
                log.msg(u'%s has added audio to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added audio' % format_identity(session.remote_identity), exclude=session)
            welcome_handler = WelcomeHandler(self, session)
            welcome_handler.start(welcome_prompt=False)
        elif notification.data.action == 'remove':
            try:
                chat_stream = (stream for stream in streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                notification_center.remove_observer(self, sender=chat_stream)
                log.msg(u'%s has removed chat from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed chat' % format_identity(session.remote_identity), exclude=session)
            try:
                audio_stream = (stream for stream in streams if stream.type == 'audio').next()
            except StopIteration:
                pass
            else:
                notification_center.remove_observer(self, sender=audio_stream)
                try:
                    self.audio_conference.remove(audio_stream)
                except ValueError:
                    # User may hangup before getting bridged into the conference
                    pass
                if len(self.audio_conference.streams) == 0:
                    self.moh_player.pause()
                    self.audio_conference.hold()
                elif len(self.audio_conference.streams) == 1:
                    self.moh_player.play()
                log.msg(u'%s has removed audio from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed audio' % format_identity(session.remote_identity), exclude=session)
            if not session.streams:
                log.msg(u'%s has removed all streams from %s, session will be terminated' % (format_identity(session.remote_identity), self.uri))
                session.end()
        self.dispatch_conference_info()

    @staticmethod
    def format_stream_types(streams):
        if not streams:
            return ''
        if len(streams) == 1:
            txt = 'with %s' % streams[0].type
        else:
            txt = 'with %s' % ','.join(stream.type for stream in streams[:-1])
            txt += ' and %s' % streams[-1:][0].type
        return txt

    @staticmethod
    def format_conference_stream_type(stream):
        if stream.type == 'chat':
            return 'message'
        return stream.type

    @staticmethod
    def format_session_duration(session):
        if session.start_time:
            duration = session.end_time - session.start_time
            seconds = duration.seconds if duration.microseconds < 500000 else duration.seconds+1
            minutes, seconds = seconds / 60, seconds % 60
            hours, minutes = minutes / 60, minutes % 60
            hours += duration.days*24
            if not minutes and not hours:
                duration_text = '%d seconds' % seconds
            elif not hours:
                duration_text = '%02d:%02d' % (minutes, seconds)
            else:
                duration_text = '%02d:%02d:%02d' % (hours, minutes, seconds)
        else:
            duration_text = '0s'
        return duration_text

    @staticmethod
    def format_file_size(size):
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                        (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                        (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                        (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size


class MoHPlayer(object):
    implements(IObserver)

    def __init__(self, conference):
        self.conference = conference
        self.files = None
        self.paused = True
        self._player = None

    def initialize(self):
        files = glob('%s/*.wav' % ResourcePath('sounds/moh').normalized)
        if not files:
            log.error(u'No files found, MoH is disabled')
            return
        random.shuffle(files)
        self.files = cycle(files)
        self._player = WavePlayer(SIPApplication.voice_audio_mixer, '', pause_time=1, initial_play=False, volume=20)
        self.conference.bridge.add(self._player)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._player)

    def stop(self):
        if self._player is None:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._player)
        self._player.stop()
        self.conference.bridge.remove(self._player)
        self.conference = None

    def play(self):
        if self._player is not None and self.paused:
            self.paused = False
            self._play_next_file()
            log.msg(u'Started playing music on hold')

    def pause(self):
        if self._player is not None and not self.paused:
            self.paused = True
            self._player.stop()
            log.msg(u'Stopped playing music on hold')

    def _play_next_file(self):
        self._player.filename = self.files.next()
        self._player.play()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_WavePlayerDidFail(self, notification):
        if not self.paused:
            self._play_next_file()

    def _NH_WavePlayerDidEnd(self, notification):
        if not self.paused:
            self._play_next_file()


class InterruptWelcome(Exception): pass

class WelcomeHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self.room = room
        self.session = session
        self.procs = proc.RunningProcSet()

    @run_in_green_thread
    def start(self, welcome_prompt=True):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        self.procs.spawn(self.play_audio_welcome, welcome_prompt)
        self.procs.spawn(self.render_chat_welcome, welcome_prompt)
        self.procs.waitall()

        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.room = None

    def play_file_in_player(self, player, file, delay):
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError, e:
            log.warning(u"Error playing file %s: %s" % (file, e))

    def play_audio_welcome(self, welcome_prompt):
        try:
            audio_stream = (stream for stream in self.session.streams if stream.type == 'audio').next()
        except StopIteration:
            return
        try:
            player = WavePlayer(audio_stream.mixer, '', pause_time=1, initial_play=False, volume=50)
            audio_stream.bridge.add(player)
            if welcome_prompt:
                file = ResourcePath('sounds/co_welcome_conference.wav').normalized
                self.play_file_in_player(player, file, 1)
            user_count = len(set(str(s.remote_identity.uri) for s in self.room.sessions if any(stream for stream in s.streams if stream.type == 'audio')) - set([str(self.session.remote_identity.uri)]))
            if user_count == 0:
                file = ResourcePath('sounds/co_only_one.wav').normalized
                self.play_file_in_player(player, file, 0.5)
            elif user_count == 1:
                file = ResourcePath('sounds/co_there_is.wav').normalized
                self.play_file_in_player(player, file, 0.5)
            elif user_count < 100:
                file = ResourcePath('sounds/co_there_are.wav').normalized
                self.play_file_in_player(player, file, 0.2)
                if user_count <= 24:
                    file = ResourcePath('sounds/bi_%d.wav' % user_count).normalized
                    self.play_file_in_player(player, file, 0.1)
                else:
                    file = ResourcePath('sounds/bi_%d0.wav' % (user_count / 10)).normalized
                    self.play_file_in_player(player, file, 0.1)
                    file = ResourcePath('sounds/bi_%d.wav' % (user_count % 10)).normalized
                    self.play_file_in_player(player, file, 0.1)
                file = ResourcePath('sounds/co_more_participants.wav').normalized
                self.play_file_in_player(player, file, 0)
            file = ResourcePath('sounds/connected_tone.wav').normalized
            self.play_file_in_player(player, file, 0.1)
            audio_stream.bridge.remove(player)
        except InterruptWelcome:
            try:
                audio_stream.bridge.remove(player)
            except ValueError:
                pass
        else:
            self.room.audio_conference.add(audio_stream)
            self.room.audio_conference.unhold()
            if len(self.room.audio_conference.streams) == 1:
                self.room.moh_player.play()
            else:
                self.room.moh_player.pause()

    def render_chat_welcome_prompt(self):
        txt = 'Welcome to the conference.'
        user_count = len(set(str(s.remote_identity.uri) for s in self.room.sessions) - set([str(self.session.remote_identity.uri)]))
        if user_count == 0:
            txt += ' You are the first participant in the room.'
        else:
            if user_count == 1:
                txt += ' There is one more participant in the room.'
            else:
                txt += ' There are %s more participants in the room.' % user_count
        return txt

    def render_chat_welcome(self, welcome_prompt):
        try:
            chat_stream = (stream for stream in self.session.streams if stream.type == 'chat').next()
        except StopIteration:
            return
        try:
            #welcome_prompt = self.render_chat_welcome_prompt()
            #chat_stream.send_message(welcome_prompt, 'text/plain', local_identity=self.room.identity, recipients=[self.room.identity])
            remote_identity = CPIMIdentity.parse(format_identity(self.session.remote_identity, cpim_format=True))
            for msg in database.get_last_messages(self.room.uri, ConferenceConfig.replay_history):
                recipient = CPIMIdentity.parse(msg.cpim_recipient)
                sender = CPIMIdentity.parse(msg.cpim_sender)
                if recipient.uri in (self.room.identity.uri, remote_identity.uri) or sender.uri == remote_identity.uri:
                    chat_stream.send_message(msg.cpim_body, msg.cpim_content_type, local_identity=sender, recipients=[recipient], timestamp=msg.cpim_timestamp)
        except InterruptWelcome:
            pass

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionWillEnd(self, notification):
        self.procs.killall(InterruptWelcome)


class RoomFile(object):

    def __init__(self, name, hash, size, sender, status):
        self.name = name
        self.hash = hash
        self.size = size
        self.sender = sender
        self.status = status

    @property
    def file_selector(self):
        return FileSelector.for_file(self.name.encode('utf-8'), hash=self.hash)


class IncomingFileTransferHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self.room = room
        self.session = session
        self.stream = (stream for stream in self.session.streams if stream.type == 'file-transfer').next()
        self.error = False
        self.ended = False
        self.file = None
        self.file_selector = None
        self.filename = None
        self.hash = None
        self.status = None
        self.timer = None
        self.transfer_finished = False

    def start(self):
        self.file_selector = self.stream.file_selector
        path = os.path.join(ConferenceConfig.file_transfer_dir, self.room.uri)
        makedirs(path)
        self.filename = filename = os.path.join(path, self.file_selector.name.decode('utf-8'))
        basename, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filename):
            filename = '%s_%d%s' % (self.filename, i, ext)
            i += 1
        self.filename = filename
        try:
            self.file = open(self.filename, 'wb')
        except EnvironmentError:
            log.msg('Cannot write destination filename: %s' % self.filename)
            self.session.end()
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)
        self.hash = hashlib.sha1()

    @run_in_thread('file-transfer')
    def write_chunk(self, data):
        notification_center = NotificationCenter()
        if data is not None:
            try:
                self.file.write(data)
            except EnvironmentError, e:
                notification_center.post_notification('IncomingFileTransferHandlerGotError', sender=self, data=TimestampedNotificationData(error=str(e)))
            else:
                self.hash.update(data)
        else:
            self.file.close()
            if self.error:
                notification_center.post_notification('IncomingFileTransferHandlerDidFail', sender=self, data=TimestampedNotificationData())
            else:
                notification_center.post_notification('IncomingFileTransferHandlerDidEnd', sender=self, data=TimestampedNotificationData())

    @run_in_thread('file-io')
    def remove_bogus_file(self, filename):
        try:
            os.unlink(filename)
        except OSError:
            pass

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        self.ended = True
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None

        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)

        # Mark end of write operation
        self.write_chunk(None)

    def _NH_FileTransferStreamGotChunk(self, notification):
        self.write_chunk(notification.data.content)

    def _NH_FileTransferStreamDidFinish(self, notification):
        self.transfer_finished = True
        if self.timer is None:
            self.timer = reactor.callLater(5, self.session.end)

    def _NH_IncomingFileTransferHandlerGotError(self, notification):
        log.error('Error while handling incoming file transfer: %s' % notification.data.error)
        self.error = True
        self.status = notification.data.error
        if not self.ended and self.timer is None:
            self.timer = reactor.callLater(5, self.session.end)

    def _NH_IncomingFileTransferHandlerDidEnd(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self)

        if not self.transfer_finished:
            log.msg('File transfer of %s cancelled' % os.path.basename(self.filename))
            self.remove_bogus_file(self.filename)
        else:
            local_hash = 'sha1:' + ':'.join(re.findall(r'..', self.hash.hexdigest().upper()))
            remote_hash = self.file_selector.hash
            if local_hash != remote_hash:
                log.warning('Hash of transferred file does not match the remote hash (file may have changed).')
                self.status = 'Hash missmatch'
                self.remove_bogus_file(self.filename)
            else:
                self.status = 'OK'

            file = RoomFile(self.filename, remote_hash, self.file_selector.size, format_identity(self.session.remote_identity, cpim_format=True), self.status)
            self.room.add_file(file)

        self.session = None
        self.room = None

    def _NH_IncomingFileTransferHandlerDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self)

        file = RoomFile(self.filename, self.file_selector.hash, self.file_selector.size, format_identity(self.session.remote_identity, cpim_format=True), self.status)
        self.room.add_file(file)

        self.session = None
        self.room = None


class OutgoingFileTransferRequestHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self._channel = coros.queue()
        self.room = room
        self.session = session
        self.stream = (stream for stream in self.session.streams if stream.type == 'file-transfer').next()
        self.timer = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

        while True:
            notification = self._channel.wait()
            if notification.name in ('SIPSessionDidFail', 'SIPSessionDidEnd'):
                break

        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.stream = None
        self.room = None

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_FileTransferStreamDidFinish(self, notification):
        if self.timer is None:
            self.timer = reactor.callLater(2, self.session.end)

    def _NH_SIPSessionDidFail(self, notification):
        self._channel.send(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        self._channel.send(notification)


class InterruptFileTransfer(Exception): pass

class OutgoingFileTransferHandler(object):
    implements(IObserver)

    def __init__(self, room, destination, file):
        self._channel = coros.queue()
        self.greenlet = None
        self.room = room
        self.destination = destination
        self.file = file
        self.session = None
        self.stream = None
        self.timer = None

    @run_in_green_thread
    def start(self):
        self.greenlet = api.getcurrent()
        settings = SIPSimpleSettings()
        account = AccountManager().default_account
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                            port=account.sip.outbound_proxy.port,
                            parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = SIPURI.new(self.destination)
        lookup = DNSLookup()
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except (DNSLookupError, InterruptFileTransfer):
            self.greenlet = None
            self.room = None
        else:
            notification_center = NotificationCenter()
            self.session = ServerSession(account)
            self.stream = FileTransferStream(account, self.file.file_selector, 'sendonly')
            notification_center.add_observer(self, sender=self.session)
            notification_center.add_observer(self, sender=self.stream)
            subject = u'File uploaded by %s' % self.file.sender
            from_header = FromHeader(SIPURI.new(self.room.identity.uri), u'Conference File Transfer')
            to_header = ToHeader(SIPURI.new(self.destination))
            transport = routes[0].transport
            parameters = {} if transport=='udp' else {'transport': transport}
            contact_header = ContactHeader(SIPURI(user=self.room.identity.uri.user, host=SIPConfig.local_ip, port=getattr(SIPConfig, 'local_%s_port' % transport), parameters=parameters))
            extra_headers = []
            if ThorNodeConfig.enabled:
                extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
            originator_uri = CPIMIdentity.parse(self.file.sender).uri
            extra_headers.append(Header('X-Originator-From', str(originator_uri)))
            self.session.connect(from_header, to_header, contact_header, routes=routes, streams=[self.stream], is_focus=True, subject=subject, extra_headers=extra_headers)
            try:
                while True:
                    notification = self._channel.wait()
                    if notification.name in ('SIPSessionDidFail', 'SIPSessionDidEnd'):
                        break
            except InterruptFileTransfer:
                self.session.end()
            else:
                if self.timer is not None and self.timer.active():
                    self.timer.cancel()
                self.timer = None
                self.greenlet = None
                notification_center.remove_observer(self, sender=self.stream)
                notification_center.remove_observer(self, sender=self.session)
                self.session = None
                self.stream = None
                self.room = None

    def stop(self):
        # Needs to be called from a green thread
        if self.greenlet is None:
            return
        api.kill(self.greenlet, InterruptFileTransfer)
        self.greenlet = api.getcurrent()
        while True:
            notification = self._channel.wait()
            if notification.name in ('SIPSessionDidFail', 'SIPSessionDidEnd'):
                break
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        self.greenlet = None
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.stream = None
        self.room = None

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_FileTransferStreamDidFinish(self, notification):
        if self.timer is None:
            self.timer = reactor.callLater(2, self.session.end)

    def _NH_SIPSessionDidStart(self, notification):
        self._channel.send(notification)

    def _NH_SIPSessionDidFail(self, notification):
        self._channel.send(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        self._channel.send(notification)

