from __future__ import absolute_import
import base64
import threading
import six
import re
import os
import requests
import time
import math

from kodi_six import xbmc
from kodi_six import xbmcgui
from . import backgroundthread
from . import kodijsonrpc
from . import colors
from .windows import seekdialog
from . import util
from plexnet import plexplayer
from plexnet import plexapp
from plexnet import signalsmixin
from plexnet import util as plexnetUtil
from six.moves import range

FIVE_MINUTES_MILLIS = 300000

class BasePlayerHandler(object):
    def __init__(self, player, session_id=None):
        self.player = player
        self.media = None
        self.baseOffset = 0
        self.timelineType = None
        self.lastTimelineState = None
        self.ignoreTimelines = False
        self.queuingNext = False
        self.playQueue = None
        self.sessionID = session_id

    def onAVChange(self):
        pass

    def onAVStarted(self):
        pass

    def onPrePlayStarted(self):
        pass

    def onPlayBackStarted(self):
        pass

    def onPlayBackPaused(self):
        pass

    def onPlayBackResumed(self):
        pass

    def onPlayBackStopped(self):
        pass

    def onPlayBackEnded(self):
        pass

    def onPlayBackSeek(self, stime, offset):
        pass

    def onPlayBackFailed(self):
        pass

    def onVideoWindowOpened(self):
        pass

    def onVideoWindowClosed(self):
        pass

    def onVideoOSD(self):
        pass

    def onSeekOSD(self):
        pass

    def onMonitorInit(self):
        pass

    def tick(self):
        pass

    def close(self):
        pass

    def setSubtitles(self, *args, **kwargs):
        pass

    def getIntroOffset(self, offset=None, setSkipped=False):
        pass

    def setup(self, duration, meta, offset, bif_url, **kwargs):
        pass

    @property
    def trueTime(self):
        return self.baseOffset + self.player.currentTime

    def getCurrentItem(self):
        if self.player.playerObject:
            return self.player.playerObject.item
        return None

    def shouldSendTimeline(self, item):
        return item.ratingKey and item.getServer()

    def currentDuration(self):
        if self.player.playerObject and self.player.isPlaying():
            try:
                return int(self.player.getTotalTime() * 1000)
            except RuntimeError:
                pass

        return 0

    def updateNowPlaying(self, force=False, refreshQueue=False, state=None, time=None):
        util.DEBUG_LOG("UpdateNowPlaying: force: {0} refreshQueue: {1} state: {2}".format(force, refreshQueue, state))
        if self.ignoreTimelines:
            util.DEBUG_LOG("UpdateNowPlaying: ignoring timeline as requested")
            return

        item = self.getCurrentItem()
        if not item:
            return

        if not self.shouldSendTimeline(item):
            return

        state = state or self.player.playState
        # Avoid duplicates
        if state == self.lastTimelineState and not force:
            return

        self.lastTimelineState = state
        # self.timelineTimer.reset()

        time = time or int(self.trueTime * 1000)

        # self.trigger("progress", [m, item, time])

        if refreshQueue and self.playQueue:
            self.playQueue.refreshOnTimeline = True

        plexapp.util.APP.nowplayingmanager.updatePlaybackState(
            self.timelineType, self.player.playerObject, state, time, self.playQueue, duration=self.currentDuration()
        )

    def getVolume(self):
        return util.rpc.Application.GetProperties(properties=["volume"])["volume"]


class SeekPlayerHandler(BasePlayerHandler):
    NO_SEEK = 0
    SEEK_IN_PROGRESS = 2
    SEEK_PLAYLIST = 3
    SEEK_REWIND = 4
    SEEK_POST_PLAY = 5

    MODE_ABSOLUTE = 0
    MODE_RELATIVE = 1

    def __init__(self, player, session_id=None):
        BasePlayerHandler.__init__(self, player, session_id)
        self.dialog = None
        self.playlist = None
        self.playQueue = None
        self.timelineType = 'video'
        self.ended = False
        self.bifURL = ''
        self.title = ''
        self.title2 = ''
        self.seekOnStart = 0
        self.chapters = None
        self.stoppedInBingeMode = False
        self.inBingeMode = False
        self.prePlayWitnessed = False
        self.queuingNext = False
        self.reset()

    def reset(self):
        self.duration = 0
        self.offset = 0
        self.baseOffset = 0
        self.seeking = self.NO_SEEK
        self.seekOnStart = 0
        self.mode = self.MODE_RELATIVE
        self.ended = False
        self.stoppedInBingeMode = False
        self.prePlayWitnessed = False
        self.queuingNext = False

    def setup(self, duration, meta, offset, bif_url, title='', title2='', seeking=NO_SEEK, chapters=None):
        self.ended = False
        self.baseOffset = offset / 1000.0
        self.seeking = seeking
        self.duration = duration
        self.bifURL = bif_url
        self.title = title
        self.title2 = title2
        self.chapters = chapters or []
        self.playedThreshold = plexapp.util.INTERFACE.getPlayedThresholdValue()
        self.ignoreTimelines = False
        self.queuingNext = False
        self.stoppedInBingeMode = False
        self.inBingeMode = False
        self.prePlayWitnessed = False
        self.getDialog(setup=True)
        self.dialog.setup(self.duration, meta, int(self.baseOffset * 1000), self.bifURL, self.title, self.title2,
                          chapters=self.chapters, keepMarkerDef=seeking == self.SEEK_IN_PROGRESS)

    def getDialog(self, setup=False):
        if not self.dialog:
            self.dialog = seekdialog.SeekDialog.create(show=False, handler=self)

        return self.dialog

    @property
    def isTranscoded(self):
        return self.mode == self.MODE_RELATIVE

    @property
    def isDirectPlay(self):
        return self.mode == self.MODE_ABSOLUTE

    @property
    def trueTime(self):
        if self.isTranscoded:
            return self.baseOffset + self.player.currentTime
        else:
            if not self.player.playerObject:
                return 0
            if self.seekOnStart:
                return self.player.playerObject.startOffset + (self.seekOnStart / 1000)
            else:
                return self.player.currentTime + self.player.playerObject.startOffset

    def shouldShowPostPlay(self):
        if self.playlist and self.playlist.TYPE == 'playlist':
            return False

        if self.inBingeMode and not self.stoppedInBingeMode:
            return False

        if (not util.advancedSettings.postplayAlways and self.player.video.duration.asInt() <= FIVE_MINUTES_MILLIS)\
                or util.advancedSettings.postplayTimeout <= 0:
            return False

        return True

    def showPostPlay(self):
        if not self.shouldShowPostPlay():
            util.DEBUG_LOG("SeekHandler: Not showing post-play")
            return
        util.DEBUG_LOG("SeekHandler: Showing post-play")

        self.seeking = self.SEEK_POST_PLAY
        self.hideOSD(delete=True)

        self.player.trigger('post.play', video=self.player.video, playlist=self.playlist, handler=self,
                            stoppedInBingeMode=self.stoppedInBingeMode)

        self.stoppedInBingeMode = False

        return True

    def getIntroOffset(self, offset=None, setSkipped=False):
        return self.getDialog().displayMarkers(onlyReturnIntroMD=True, offset=offset, setSkipped=setSkipped)

    def next(self, on_end=False):
        if self.playlist and next(self.playlist):
            self.seeking = self.SEEK_PLAYLIST

        if on_end:
            if self.showPostPlay():
                return True

        if not self.playlist or self.stoppedInBingeMode:
            return False

        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def prev(self):
        if not self.playlist or not self.playlist.prev():
            return False

        self.seeking = self.SEEK_PLAYLIST
        xbmc.sleep(500)
        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def playAt(self, pos):
        if not self.playlist or not self.playlist.setCurrent(pos):
            return False

        self.seeking = self.SEEK_PLAYLIST
        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def onSeekAborted(self):
        if self.seeking:
            self.seeking = self.NO_SEEK
            self.player.control('play')

    def showOSD(self, from_seek=False):
        self.updateOffset()
        if self.dialog:
            self.dialog.update(self.offset, from_seek)
            self.dialog.showOSD()

    def hideOSD(self, delete=False):
        util.CRON.forceTick()
        if self.dialog:
            self.dialog.hideOSD(closing=delete, skipMarkerFocus=True)
            if delete:
                d = self.dialog
                self.dialog = None
                d.doClose()
                del d
                util.garbageCollect()

    def seek(self, offset, settings_changed=False, seeking=SEEK_IN_PROGRESS):
        util.DEBUG_LOG(
            "SeekHandler: offset={0}, settings_changed={1}, seeking={2}, state={3}".format(offset,
                                                                                           settings_changed,
                                                                                           seeking,
                                                                                           self.player.playState))
        if offset is None:
            return

        self.offset = offset

        if self.isDirectPlay and not settings_changed:
            util.DEBUG_LOG('New absolute player offset: {0}'.format(self.offset))

            if self.player.playerObject.offsetIsValid(offset / 1000):
                if self.seekAbsolute(offset):
                    return

        self.updateNowPlaying(state=self.player.STATE_PAUSED)  # To for update after seek

        self.seeking = self.SEEK_IN_PROGRESS

        if self.player.playState == self.player.STATE_PAUSED:
            self.player.pauseAfterPlaybackStarted = True

        util.DEBUG_LOG('New player offset: {0}, state: {1}'.format(self.offset, self.player.playState))
        self.player._playVideo(offset, seeking=self.seeking, force_update=settings_changed)

    def fastforward(self):
        xbmc.executebuiltin('PlayerControl(forward)')

    def rewind(self):
        if self.isDirectPlay:
            xbmc.executebuiltin('PlayerControl(rewind)')
        else:
            self.seek(max(self.trueTime - 30, 0) * 1000, seeking=self.SEEK_REWIND)

    def seekAbsolute(self, seek=None):
        self.seekOnStart = seek or (self.seekOnStart if self.seekOnStart else None)
        if self.seekOnStart is not None:
            seekSeconds = self.seekOnStart / 1000.0
            try:
                if seekSeconds >= self.player.getTotalTime():
                    util.DEBUG_LOG("SeekAbsolute: Bad offset: {0}".format(seekSeconds))
                    return False
            except RuntimeError:  # Not playing a file
                util.DEBUG_LOG("SeekAbsolute: runtime error")
                return False
            self.updateNowPlaying(state=self.player.STATE_PAUSED)  # To for update after seek

            util.DEBUG_LOG("SeekAbsolute: Seeking to {0}".format(self.seekOnStart))
            self.player.seekTime(self.seekOnStart / 1000.0)
        return True

    def onAVChange(self):
        util.DEBUG_LOG('SeekHandler: onAVChange')
        if self.dialog:
            self.dialog.onAVChange()

    def onAVStarted(self):
        util.DEBUG_LOG('SeekHandler: onAVStarted')

        if self.isDirectPlay:
            self.seekAbsolute()

        if self.dialog:
            self.dialog.onAVStarted()

        # check if embedded subtitle was set correctly
        if self.isDirectPlay and self.player.video and self.player.video.current_subtitle_is_embedded:
            try:
                playerID = kodijsonrpc.rpc.Player.GetActivePlayers()[0]["playerid"]
                currIdx = kodijsonrpc.rpc.Player.GetProperties(playerid=playerID, properties=['currentsubtitle'])[
                    'currentsubtitle']['index']
                if currIdx != self.player.video._current_subtitle_idx:
                    util.LOG("Embedded Subtitle index was incorrect ({}), setting to: {}".
                             format(currIdx, self.player.video._current_subtitle_idx))
                    self.dialog.setSubtitles()
                else:
                    util.DEBUG_LOG("Embedded subtitle was correctly set in Kodi")
            except:
                util.ERROR("Exception when trying to check for embedded subtitles")

    def onPrePlayStarted(self):
        util.DEBUG_LOG('SeekHandler: onPrePlayStarted, DP: {}'.format(self.isDirectPlay))
        self.prePlayWitnessed = True
        if self.isDirectPlay:
            self.setSubtitles(do_sleep=False)

    def onPlayBackStarted(self):
        util.DEBUG_LOG('SeekHandler: onPlayBackStarted, DP: {}'.format(self.isDirectPlay))
        self.updateNowPlaying(force=True, refreshQueue=True)

        if self.dialog:
            self.dialog.onPlayBackStarted()

        #if not self.prePlayWitnessed and self.isDirectPlay:
        if self.isDirectPlay:
            self.setSubtitles(do_sleep=False)

    def onPlayBackResumed(self):
        self.updateNowPlaying()
        if self.dialog:
            self.dialog.onPlayBackResumed()

            util.CRON.forceTick()
        # self.hideOSD()

    def onPlayBackStopped(self):
        util.DEBUG_LOG('SeekHandler: onPlayBackStopped - '
                       'Seeking={0}, QueueingNext={1}, BingeMode={2}'.format(self.seeking, self.queuingNext,
                                                                             self.inBingeMode))

        if self.dialog:
            self.dialog.onPlayBackStopped()

        if self.queuingNext and self.inBingeMode:
            if self.isDirectPlay and self.playlist and self.playlist.hasNext():
                self.hideOSD(delete=True)
            if self.next(on_end=False):
                return

        if self.seeking not in (self.SEEK_IN_PROGRESS, self.SEEK_REWIND):
            self.updateNowPlaying()

            # show post play if possible, if an item has been watched (90% by Plex standards)
            if self.seeking != self.SEEK_PLAYLIST and self.duration:
                playedFac = self.trueTime * 1000 / float(self.duration)
                util.DEBUG_LOG("Player - played-threshold: {}/{}".format(playedFac, self.playedThreshold))
                if playedFac >= self.playedThreshold and self.next(on_end=True):
                    return

        if self.seeking not in (self.SEEK_IN_PROGRESS, self.SEEK_PLAYLIST):
            self.hideOSD(delete=True)
            self.sessionEnded()

    def onPlayBackEnded(self):
        util.DEBUG_LOG('SeekHandler: onPlayBackEnded - Seeking={0}'.format(self.seeking))

        if self.dialog:
            self.dialog.onPlayBackEnded()

        if self.player.playerObject.hasMoreParts():
            self.updateNowPlaying(state=self.player.STATE_PAUSED)  # To for update after seek
            self.seeking = self.SEEK_IN_PROGRESS
            self.player._playVideo(self.player.playerObject.getNextPartOffset(), seeking=self.seeking)
            return

        self.updateNowPlaying()

        if self.queuingNext:
            util.DEBUG_LOG('SeekHandler: onPlayBackEnded - event ignored')
            return

        if self.inBingeMode:
            self.stoppedInBingeMode = False

        if self.playlist and self.playlist.hasNext():
            self.queuingNext = True
        if self.next(on_end=True):
            return
        else:
            self.queuingNext = False

        if not self.ended:
            if self.seeking != self.SEEK_PLAYLIST:
                self.hideOSD()

            if self.seeking not in (self.SEEK_IN_PROGRESS, self.SEEK_PLAYLIST):
                self.sessionEnded()

    def onPlayBackPaused(self):
        self.updateNowPlaying()
        if self.dialog:
            self.dialog.onPlayBackPaused()

    def onPlayBackSeek(self, stime, offset):
        util.DEBUG_LOG('SeekHandler: onPlayBackSeek - {0}, {1}, {2}'.format(stime, offset, self.seekOnStart))
        if self.dialog:
            self.dialog.onPlayBackSeek(stime, offset)

        if self.seekOnStart:
            seeked = False
            if self.dialog:
                seeked = self.dialog.tick(stime)

            if seeked:
                util.DEBUG_LOG("OnPlayBackSeek: Seeked on start to: {0}".format(stime))
                self.seekOnStart = 0
            return

        self.updateOffset()
        # self.showOSD(from_seek=True)

    def setSubtitles(self, do_sleep=True, honor_forced_subtitles_override=True):
        if not self.player.video:
            util.LOG("Warning: SetSubtitles: no player.video object available")
            return

        subs = self.player.video.selectedSubtitleStream(
            forced_subtitles_override=honor_forced_subtitles_override and util.getSetting("forced_subtitles_override",
                                                                                          False))
        if subs:
            if do_sleep:
                xbmc.sleep(100)

            path = subs.getSubtitleServerPath()
            if self.isDirectPlay:
                self.player.showSubtitles(False)
                if path:
                    util.DEBUG_LOG('Setting subtitle path: {0} ({1})'.format(path, subs))
                    self.player.setSubtitles(path)
                    self.player.showSubtitles(True)

                else:
                    # u_til.TEST(subs.__dict__)
                    # u_til.TEST(self.player.video.mediaChoice.__dict__)
                    util.DEBUG_LOG('Enabling embedded subtitles at: {0} ({1})'.format(subs.typeIndex, subs))
                    self.player.setSubtitleStream(subs.typeIndex)
                    self.player.showSubtitles(True)

        else:
            self.player.showSubtitles(False)

    def setAudioTrack(self):
        if self.isDirectPlay:
            track = self.player.video.selectedAudioStream()
            if track:
                # only try finding the current audio stream when the BG music isn't playing and wasn't the last
                # thing played, because currentaudiostream doesn't populate for audio-only items; in that case,
                # always select the proper audio stream
                if not self.player.lastPlayWasBGM:
                    try:
                        playerID = kodijsonrpc.rpc.Player.GetActivePlayers()[0]["playerid"]
                        currIdx = kodijsonrpc.rpc.Player.GetProperties(playerid=playerID, properties=['currentaudiostream'])['currentaudiostream']['index']
                        if currIdx == track.typeIndex:
                            util.DEBUG_LOG('Audio track is correct index: {0}'.format(track.typeIndex))
                            return
                    except:
                        util.ERROR()

                self.player.lastPlayWasBGM = False

                xbmc.sleep(100)
                util.DEBUG_LOG('Switching audio track - index: {0}'.format(track.typeIndex))
                self.player.setAudioStream(track.typeIndex)

    def updateOffset(self):
        try:
            self.offset = int(self.player.getTime() * 1000)
        except RuntimeError:
            pass

    def initPlayback(self):
        self.seeking = self.NO_SEEK

        #self.setSubtitles()
        if self.isTranscoded and self.player.getAvailableSubtitleStreams():
            util.DEBUG_LOG('Enabling first subtitle stream, as we\'re in DirectStream')
            self.player.showSubtitles(True)
        self.setAudioTrack()

    def onPlayBackFailed(self):
        if self.ended:
            return False

        if self.dialog:
            self.dialog.onPlayBackFailed()

        util.DEBUG_LOG('SeekHandler: onPlayBackFailed - Seeking={0}'.format(self.seeking))
        if self.seeking not in (self.SEEK_IN_PROGRESS, self.SEEK_PLAYLIST):
            self.sessionEnded()

        if self.seeking == self.SEEK_IN_PROGRESS:
            return False
        else:
            self.seeking = self.NO_SEEK

        return True

    # def onSeekOSD(self):
    #     self.dialog.activate()

    def onVideoWindowOpened(self):
        util.DEBUG_LOG('SeekHandler: onVideoWindowOpened - Seeking={0}'.format(self.seeking))
        self.getDialog().show()

        self.initPlayback()

    def onVideoWindowClosed(self):
        self.hideOSD()
        util.DEBUG_LOG('SeekHandler: onVideoWindowClosed - Seeking={0}'.format(self.seeking))
        if not self.seeking:
            if self.player.isPlaying():
                self.player.stop()
            if not self.playlist or not self.playlist.hasNext():
                if not self.shouldShowPostPlay():
                    self.sessionEnded()

    def onVideoOSD(self):
        # xbmc.executebuiltin('Dialog.Close(seekbar,true)')  # Doesn't work :)
        self.showOSD()

    def tick(self):
        if self.seeking != self.SEEK_IN_PROGRESS:
            self.updateNowPlaying(force=True)

        if self.dialog and getattr(self.dialog, "_ignoreTick", None) is not True:
            self.dialog.tick()

    def close(self):
        self.hideOSD(delete=True)

    def sessionEnded(self):
        if self.ended:
            return
        self.ended = True
        util.DEBUG_LOG('Player: Video session ended')
        self.player.trigger('session.ended', session_id=self.sessionID)
        self.hideOSD(delete=True)

    __next__ = next


class AudioPlayerHandler(BasePlayerHandler):
    def __init__(self, player):
        BasePlayerHandler.__init__(self, player)
        self.timelineType = 'music'
        util.setGlobalProperty('track.ID', '')
        self.extractTrackInfo()

    def extractTrackInfo(self):
        if not self.player.isPlayingAudio():
            return

        plexID = None
        for x in range(10):  # Wait a sec (if necessary) for this to become available
            try:
                item = kodijsonrpc.rpc.Player.GetItem(playerid=0, properties=['comment'])['item']
                plexID = item['comment']
            except:
                util.ERROR()

            if plexID:
                break
            xbmc.sleep(100)

        if not plexID:
            return

        if not plexID.startswith('PLEX-'):
            return

        util.DEBUG_LOG('Extracting track info from comment')
        try:
            data = plexID.split(':', 1)[-1]
            from plexnet import plexobjects
            track = plexobjects.PlexObject.deSerialize(base64.urlsafe_b64decode(data.encode('utf-8')))
            track.softReload()
            self.media = track
            pobj = plexplayer.PlexAudioPlayer(track)
            self.player.playerObject = pobj
            self.updatePlayQueueTrack(track)
            util.setGlobalProperty('track.ID', track.ratingKey)  # This is used in the skins to match a listitem
        except:
            util.ERROR()

    def setPlayQueue(self, pq):
        self.playQueue = pq
        pq.on('items.changed', self.playQueueCallback)

    def playQueueCallback(self, **kwargs):
        plist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        # plist.clear()
        try:
            citem = kodijsonrpc.rpc.Player.GetItem(playerid=0, properties=['comment'])['item']
            plexID = citem['comment'].split(':', 1)[0]
        except:
            util.ERROR()
            return

        current = plist.getposition()
        size = plist.size()

        # Remove everything but the current track
        for x in range(size - 1, current, -1):  # First everything with a greater position
            kodijsonrpc.rpc.Playlist.Remove(playlistid=xbmc.PLAYLIST_MUSIC, position=x)
        for x in range(current):  # Then anything with a lesser position
            kodijsonrpc.rpc.Playlist.Remove(playlistid=xbmc.PLAYLIST_MUSIC, position=0)

        swap = None
        for idx, track in enumerate(self.playQueue.items()):
            tid = 'PLEX-{0}'.format(track.ratingKey)
            if tid == plexID:
                # Save the position of the current track in the pq
                swap = idx

            url, li = self.player.createTrackListItem(track, index=idx + 1)

            plist.add(url, li)

        plist[0].setInfo('music', {
            'playcount': swap + 1,
        })

        # Now swap the track to the correct position. This seems to be the only way to update the kodi playlist position to the current track's new position
        if swap is not None:
            kodijsonrpc.rpc.Playlist.Swap(playlistid=xbmc.PLAYLIST_MUSIC, position1=0, position2=swap + 1)
            kodijsonrpc.rpc.Playlist.Remove(playlistid=xbmc.PLAYLIST_MUSIC, position=0)

        self.player.trigger('playlist.changed')

    def updatePlayQueue(self, delay=False):
        if not self.playQueue:
            return

        self.playQueue.refresh(delay=delay)

    def updatePlayQueueTrack(self, track):
        if not self.playQueue:
            return

        self.playQueue.selectedId = track.playQueueItemID or None

    @property
    def trueTime(self):
        try:
            return self.player.getTime()
        except:
            return self.player.currentTime

    def stampCurrentTime(self):
        try:
            self.player.currentTime = self.player.getTime()
        except RuntimeError:  # Not playing
            pass

    def onMonitorInit(self):
        self.extractTrackInfo()
        self.updateNowPlaying(state='playing')

    def onPlayBackStarted(self):
        self.player.lastPlayWasBGM = False
        self.updatePlayQueue(delay=True)
        self.extractTrackInfo()
        self.updateNowPlaying(state='playing')

    def onPlayBackResumed(self):
        self.updateNowPlaying(state='playing')

    def onPlayBackPaused(self):
        self.updateNowPlaying(state='paused')

    def onPlayBackStopped(self):
        self.updatePlayQueue()
        self.updateNowPlaying(state='stopped')
        self.finish()

    def onPlayBackEnded(self):
        self.updatePlayQueue()
        self.updateNowPlaying(state='stopped')
        self.finish()

    def onPlayBackFailed(self):
        return True

    def finish(self):
        self.player.trigger('session.ended')
        util.setGlobalProperty('track.ID', '')

    def tick(self):
        if not self.player.isPlayingAudio() or util.MONITOR.abortRequested():
            return

        self.stampCurrentTime()
        self.updateNowPlaying(force=True)


class BGMPlayerHandler(BasePlayerHandler):
    def __init__(self, player, rating_key):
        BasePlayerHandler.__init__(self, player)
        self.timelineType = 'music'
        self.currentlyPlaying = rating_key
        util.setGlobalProperty('track.ID', '')
        util.setGlobalProperty('theme_playing', '1')

        self.oldVolume = util.rpc.Application.GetProperties(properties=["volume"])["volume"]

    def onPlayBackStarted(self):
        util.DEBUG_LOG("BGM: playing theme for %s" % self.currentlyPlaying)
        self.player.bgmPlaying = True

    def _setVolume(self, vlm):
        xbmc.executebuiltin("SetVolume({})".format(vlm))

    def setVolume(self, volume=None, reset=False):
        vlm = self.oldVolume if reset else volume
        curVolume = self.getVolume()

        if curVolume != vlm:
            util.DEBUG_LOG("BGM: {}setting volume to: {}".format("re-" if reset else "", vlm))
            self._setVolume(vlm)
        else:
            util.DEBUG_LOG("BGM: Volume already at {}".format(vlm))
            return

        waited = 0
        waitMax = 5
        while curVolume != vlm and waited < waitMax:
            util.DEBUG_LOG("Waiting for volume to change from {} to {}".format(curVolume, vlm))
            xbmc.sleep(100)
            waited += 1
            curVolume = self.getVolume()

        if waited == waitMax:
            util.DEBUG_LOG("BGM: Timeout setting volume to {} (is: {}). Might have been externally changed in the "
                           "meantime".format(vlm, self.getVolume()))

    def resetVolume(self):
        self.setVolume(reset=True)

    def onPlayBackStopped(self):
        util.DEBUG_LOG("BGM: stopped theme for {}".format(self.currentlyPlaying))
        util.setGlobalProperty('theme_playing', '')
        self.player.bgmPlaying = False
        self.resetVolume()

    def onPlayBackEnded(self):
        self.onPlayBackStopped()

    def onPlayBackFailed(self):
        self.onPlayBackStopped()

    def close(self):
        self.player.stopAndWait()
        self.onPlayBackStopped()


class BGMPlayerTask(backgroundthread.Task):
    def setup(self, source, player, *args, **kwargs):
        self.source = source
        self.player = player
        return self

    def cancel(self):
        self.player.stopAndWait()
        self.player = None
        backgroundthread.Task.cancel(self)

    def run(self):
        if self.isCanceled():
            return

        self.player.play(self.source, windowed=True)


class PlexPlayer(xbmc.Player, signalsmixin.SignalsMixin):
    STATE_STOPPED = "stopped"
    STATE_PLAYING = "playing"
    STATE_PAUSED = "paused"
    STATE_BUFFERING = "buffering"

    OFFSET_RE = re.compile(r'(offset=)\d+')

    def __init__(self, *args, **kwargs):
        xbmc.Player.__init__(self, *args, **kwargs)
        signalsmixin.SignalsMixin.__init__(self)
        self.handler = AudioPlayerHandler(self)

    def init(self):
        self._closed = False
        self._nextItem = None
        self.started = False
        self.bgmPlaying = False
        self.lastPlayWasBGM = False
        self.BGMTask = None
        self.pauseAfterPlaybackStarted = False
        self.video = None
        self.hasOSD = False
        self.hasSeekOSD = False
        self.handler = AudioPlayerHandler(self)
        self.playerObject = None
        self.currentTime = 0
        self.thread = None
        self.ignoreStopEvents = False
        if xbmc.getCondVisibility('Player.HasMedia'):
            self.started = True
        self.resume = False
        self.open()

        return self

    def open(self):
        self._closed = False
        self.monitor()

    def close(self, shutdown=False):
        self._closed = True

    def reset(self):
        self.video = None
        self.started = False
        self.bgmPlaying = False
        self.playerObject = None
        self.pauseAfterPlaybackStarted = False
        self.ignoreStopEvents = False
        #self.handler = AudioPlayerHandler(self)
        self.currentTime = 0

    def control(self, cmd):
        if cmd == 'play':
            self.pauseAfterPlaybackStarted = False
            util.DEBUG_LOG('Player - Control:  Command=Play')
            if xbmc.getCondVisibility('Player.Paused | !Player.Playing'):
                util.DEBUG_LOG('Player - Control:  Playing')
                xbmc.executebuiltin('PlayerControl(Play)')
        elif cmd == 'pause':
            util.DEBUG_LOG('Player - Control:  Command=Pause')
            if not xbmc.getCondVisibility('Player.Paused'):
                util.DEBUG_LOG('Player - Control:  Pausing')
                xbmc.executebuiltin('PlayerControl(Play)')

    @property
    def playState(self):
        if xbmc.getCondVisibility('Player.Playing'):
            return self.STATE_PLAYING
        elif xbmc.getCondVisibility('Player.Caching'):
            return self.STATE_BUFFERING
        elif xbmc.getCondVisibility('Player.Paused'):
            return self.STATE_PAUSED

        return self.STATE_STOPPED

    def videoIsFullscreen(self):
        return xbmc.getCondVisibility('VideoPlayer.IsFullscreen')

    def currentTrack(self):
        if self.handler.media and self.handler.media.type == 'track':
            return self.handler.media
        return None

    def playAt(self, path, ms):
        """
        Plays the video specified by path.
        Optionally set the start position with h,m,s,ms keyword args.
        """
        seconds = ms / 1000.0

        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)

        kodijsonrpc.rpc.Player.Open(
            item={'file': path},
            options={'resume': {'hours': h, 'minutes': m, 'seconds': s, 'milliseconds': ms}}
        )

    def play(self, *args, **kwargs):
        self.started = False
        xbmc.Player.play(self, *args, **kwargs)

    def playBackgroundMusic(self, source, volume, rating_key, *args, **kwargs):
        if self.isPlaying():
            if not self.lastPlayWasBGM:
                return

            else:
                # don't re-queue the currently playing theme
                if self.handler.currentlyPlaying == rating_key:
                    return

                # cancel any currently playing theme before starting the new one
                else:
                    self.stopAndWait()

        if self.BGMTask and self.BGMTask.isValid():
            self.BGMTask.cancel()

        self.started = False
        self.handler = BGMPlayerHandler(self, rating_key)

        # store current volume if it's different from the BGM volume
        curVol = self.handler.getVolume()
        if volume < curVol:
            util.setSetting('last_good_volume', curVol)

        self.lastPlayWasBGM = True

        self.handler.setVolume(volume)

        self.BGMTask = BGMPlayerTask().setup(source, self, *args, **kwargs)
        backgroundthread.BGThreader.addTask(self.BGMTask)

    def playVideo(self, video, resume=False, force_update=False, session_id=None, handler=None):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = handler if handler and isinstance(handler, SeekPlayerHandler) \
            else SeekPlayerHandler(self, session_id)

        self.video = video
        self.resume = resume
        self.open()
        self._playVideo(resume and video.viewOffset.asInt() or 0, force_update=force_update)

    def getOSSPathHint(self, meta):
        # only hint the path one folder above for a movie, two folders above for TV
        try:
            head1, tail1 = os.path.split(meta.path)
            head2, tail2 = os.path.split(head1)
            if self.video.type == "episode":
                head3, tail3 = os.path.split(head2)
                cleaned_path = os.path.join(tail3, tail2, tail1)
            else:
                cleaned_path = os.path.join(tail2, tail1)
        except:
            cleaned_path = ""
        return cleaned_path

    def _playVideo(self, offset=0, seeking=0, force_update=False, playerObject=None):
        self.trigger('new.video', video=self.video)
        self.trigger(
            'change.background',
            url=self.video.defaultArt.asTranscodedImageURL(1920, 1080, opacity=60, background=colors.noAlpha.Background)
        )
        try:
            if not playerObject:
                self.playerObject = plexplayer.PlexPlayer(self.video, offset, forceUpdate=force_update)
                self.playerObject.build()
            self.playerObject = self.playerObject.getServerDecision()
        except plexplayer.DecisionFailure as e:
            util.showNotification(e.reason, header=util.T(32448, 'Playback Failed!'))
            return
        except:
            util.ERROR(notify=True)
            return

        meta = self.playerObject.metadata

        # Kodi 19 will try to look for subtitles in the directory containing the file. '/' and `/file.mkv` both point
        # to the file, and Kodi will happily try to read the whole file without recognizing it isn't a directory.
        # To get around that, we omit the filename here since it is unnecessary.
        url = meta.streamUrls[0].replace("file.mkv", "").replace("file.mp4", "")

        bifURL = self.playerObject.getBifUrl()
        util.DEBUG_LOG('Playing URL(+{1}ms): {0}{2}'.format(plexnetUtil.cleanToken(url), offset, bifURL and ' - indexed' or ''))

        self.ignoreStopEvents = True
        self.stopAndWait()  # Stop before setting up the handler to prevent player events from causing havoc
        if self.handler and self.handler.queuingNext and util.advancedSettings.consecutiveVideoPbWait:
            util.DEBUG_LOG(
                "Waiting for {}s until playing back next item".format(util.advancedSettings.consecutiveVideoPbWait))
            util.MONITOR.waitForAbort(util.advancedSettings.consecutiveVideoPbWait)

        self.ignoreStopEvents = False

        self.handler.setup(self.video.duration.asInt(), meta, offset, bifURL, title=self.video.grandparentTitle,
                           title2=self.video.title, seeking=seeking, chapters=self.video.chapters)

        # try to get an early intro offset so we can skip it if necessary
        introOffset = None
        if not offset:
            # in case we're transcoded, instruct the marker handler to set the marker a skipped, so we don't re-skip it
            # after seeking
            probOff = self.handler.getIntroOffset(offset, setSkipped=meta.isTranscoded)
            if probOff:
                introOffset = probOff

        if meta.isTranscoded:
            self.handler.mode = self.handler.MODE_RELATIVE

            if introOffset:
                # cheat our way into an early intro skip by modifying the offset in the stream URL
                util.DEBUG_LOG("Immediately seeking behind intro: {}".format(introOffset))
                url = self.OFFSET_RE.sub(r"\g<1>{}".format(introOffset // 1000), url)
                self.handler.dialog.baseOffset = introOffset

                # probably not necessary
                meta.playStart = introOffset // 1000
        else:
            if offset:
                util.DEBUG_LOG("Using as SeekOnStart: {0}; offset: {1}".format(meta.playStart, offset))
                self.handler.seekOnStart = meta.playStart * 1000
            elif introOffset:
                util.DEBUG_LOG("Seeking behind intro after playstart: {}".format(introOffset))
                self.handler.seekOnStart = introOffset

            self.handler.mode = self.handler.MODE_ABSOLUTE

        url = util.addURLParams(url, {
            'X-Plex-Client-Profile-Name': 'Generic',
            'X-Plex-Client-Identifier': plexapp.util.INTERFACE.getGlobal('clientIdentifier')
        })
        li = xbmcgui.ListItem(self.video.title, path=url)
        vtype = self.video.type if self.video.type in ('movie', 'episode', 'musicvideo') else 'video'

        util.setGlobalProperty("current_path", self.getOSSPathHint(meta), base='videoinfo.{0}')
        util.setGlobalProperty("current_size", str(meta.size), base='videoinfo.{0}')
        li.setInfo('video', {
            'mediatype': vtype,
            'title': self.video.title,
            'originaltitle': self.video.title,
            'tvshowtitle': self.video.grandparentTitle,
            'episode': vtype == "episode" and self.video.index.asInt() or '',
            'season': vtype == "episode" and self.video.parentIndex.asInt() or '',
            #'year': self.video.year.asInt(),
            'plot': self.video.summary,
            'path': meta.path,
            'size': meta.size,
        })
        li.setArt({
            'poster': self.video.defaultThumb.asTranscodedImageURL(347, 518),
            'fanart': self.video.defaultArt.asTranscodedImageURL(1920, 1080),
            'thumb': self.video.defaultThumb.asTranscodedImageURL(256, 256),
        })

        self.play(url, li)

    def playVideoPlaylist(self, playlist, resume=False, handler=None, session_id=None):
        if self.bgmPlaying:
            self.stopAndWait()

        if handler and isinstance(handler, SeekPlayerHandler):
            self.handler = handler
        else:
            self.handler = SeekPlayerHandler(self, session_id)

        self.handler.playlist = playlist
        if playlist.isRemote:
            self.handler.playQueue = playlist
        self.video = playlist.current()
        self.video.softReload(includeChapters=1)
        self.resume = resume
        self.open()
        self._playVideo(resume and self.video.viewOffset.asInt() or 0, seeking=handler and handler.SEEK_PLAYLIST or 0, force_update=True)

    # def createVideoListItem(self, video, index=0):
    #     url = 'plugin://script.plex/play?{0}'.format(base64.urlsafe_b64encode(video.serialize()))
    #     li = xbmcgui.ListItem(self.video.title, path=url, thumbnailImage=self.video.defaultThumb.asTranscodedImageURL(256, 256))
    #     vtype = self.video.type if self.video.vtype in ('movie', 'episode', 'musicvideo') else 'video'
    #     li.setInfo('video', {
    #         'mediatype': vtype,
    #         'playcount': index,
    #         'title': video.title,
    #         'tvshowtitle': video.grandparentTitle,
    #         'episode': video.index.asInt(),
    #         'season': video.parentIndex.asInt(),
    #         'year': video.year.asInt(),
    #         'plot': video.summary
    #     })
    #     li.setArt({
    #         'poster': self.video.defaultThumb.asTranscodedImageURL(347, 518),
    #         'fanart': self.video.defaultArt.asTranscodedImageURL(1920, 1080),
    #     })

    #     return url, li

    def playAudio(self, track, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        url, li = self.createTrackListItem(track, fanart)
        self.stopAndWait()
        self.play(url, li, **kwargs)

    def playAlbum(self, album, startpos=-1, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        plist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        plist.clear()
        index = 1
        for track in album.tracks():
            url, li = self.createTrackListItem(track, fanart, index=index)
            plist.add(url, li)
            index += 1
        xbmc.executebuiltin('PlayerControl(RandomOff)')
        self.stopAndWait()
        self.play(plist, startpos=startpos, **kwargs)

    def playAudioPlaylist(self, playlist, startpos=-1, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        plist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        plist.clear()
        index = 1
        for track in playlist.items():
            url, li = self.createTrackListItem(track, fanart, index=index)
            plist.add(url, li)
            index += 1

        if playlist.isRemote:
            self.handler.setPlayQueue(playlist)
        else:
            if playlist.startShuffled:
                plist.shuffle()
                xbmc.executebuiltin('PlayerControl(RandomOn)')
            else:
                xbmc.executebuiltin('PlayerControl(RandomOff)')
        self.stopAndWait()
        self.play(plist, startpos=startpos, **kwargs)

    def createTrackListItem(self, track, fanart=None, index=0):
        data = base64.urlsafe_b64encode(track.serialize().encode("utf8")).decode("utf8")
        url = 'plugin://script.zidooplexmod/play?{0}'.format(data)
        li = xbmcgui.ListItem(track.title, path=url)
        li.setInfo('music', {
            'artist': six.text_type(track.originalTitle or track.grandparentTitle),
            'title': six.text_type(track.title),
            'album': six.text_type(track.parentTitle),
            'discnumber': track.parentIndex.asInt(),
            'tracknumber': track.get('index').asInt(),
            'duration': int(track.duration.asInt() / 1000),
            'playcount': index,
            'comment': 'PLEX-{0}:{1}'.format(track.ratingKey, data)
        })
        art = fanart or track.defaultArt
        li.setArt({
            'fanart': art.asTranscodedImageURL(1920, 1080),
            'landscape': util.backgroundFromArt(art),
            'thumb': track.defaultThumb.asTranscodedImageURL(800, 800),
        })
        if fanart:
            li.setArt({'fanart': fanart})
        return (url, li)

    def onPrePlayStarted(self):
        util.DEBUG_LOG('Player - PRE-PLAY; handler: %r' % self.handler)
        self.trigger('preplay.started')
        if not self.handler:
            return
        self.handler.onPrePlayStarted()

    def onPlayBackStarted(self):
        util.DEBUG_LOG('Player - STARTED')
        self.trigger('playback.started')
        self.started = True
        if self.pauseAfterPlaybackStarted:
            self.control('pause')
            self.pauseAfterPlaybackStarted = False

        if not self.handler:
            return
        self.handler.onPlayBackStarted()

    def onAVChange(self):
        util.DEBUG_LOG('Player - AVChange')
        if not self.handler:
            return
        self.handler.onAVChange()

    def onAVStarted(self):
        util.DEBUG_LOG('Player - AVStarted: {}'.format(self.handler))
        self.trigger('av.started')
        if not self.handler:
            return
        self.handler.onAVStarted()

    def onPlayBackPaused(self):
        util.DEBUG_LOG('Player - PAUSED')
        if not self.handler:
            return
        self.handler.onPlayBackPaused()

    def onPlayBackResumed(self):
        util.DEBUG_LOG('Player - RESUMED')
        if not self.handler:
            return

        self.handler.onPlayBackResumed()

    def onPlayBackStopped(self):
        util.DEBUG_LOG('Player - STOPPED' + (not self.started and ': FAILED' or ''))
        if self.ignoreStopEvents:
            return

        if not self.started:
            self.onPlayBackFailed()

        if not self.handler:
            return
        self.handler.onPlayBackStopped()

    def onPlayBackEnded(self):
        util.DEBUG_LOG('Player - ENDED' + (not self.started and ': FAILED' or ''))
        if self.ignoreStopEvents:
            return

        if not self.started:
            self.onPlayBackFailed()

        if not self.handler:
            return
        self.handler.onPlayBackEnded()

    def onPlayBackSeek(self, time, offset):
        util.DEBUG_LOG('Player - SEEK: %i' % offset)
        if not self.handler:
            return
        self.handler.onPlayBackSeek(time, offset)

    def onPlayBackFailed(self):
        util.DEBUG_LOG('Player - FAILED: {}'.format(self.handler))
        if not self.handler:
            return

        if self.handler.onPlayBackFailed():
            util.showNotification(util.T(32448, 'Playback Failed!'))
            self.stopAndWait()
            self.close()
            # xbmcgui.Dialog().ok('Failed', 'Playback failed')

    def onVideoWindowOpened(self):
        util.DEBUG_LOG('Player: Video window opened')
        try:
            self.handler.onVideoWindowOpened()
        except:
            util.ERROR()

    def onVideoWindowClosed(self):
        util.DEBUG_LOG('Player: Video window closed')
        try:
            self.handler.onVideoWindowClosed()
            # self.stop()
        except:
            util.ERROR()

    def onVideoOSD(self):
        util.DEBUG_LOG('Player: Video OSD opened')
        try:
            self.handler.onVideoOSD()
        except:
            util.ERROR()

    def onSeekOSD(self):
        util.DEBUG_LOG('Player: Seek OSD opened')
        try:
            self.handler.onSeekOSD()
        except:
            util.ERROR()

    def stopAndWait(self):
        if self.isPlaying():
            util.DEBUG_LOG('Player: Stopping and waiting...')
            self.stop()
            while not util.MONITOR.waitForAbort(0.1) and self.isPlaying():
                pass
            util.MONITOR.waitForAbort(0.2)
            util.DEBUG_LOG('Player: Stopping and waiting...Done')

    def monitor(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._monitor, name='PLAYER:MONITOR')
            self.thread.start()

    def _monitor(self):
        try:
            while not util.MONITOR.abortRequested() and not self._closed:
                if not self.isPlaying():
                    util.DEBUG_LOG('Player: Idling...')

                while not self.isPlaying() and not util.MONITOR.abortRequested() and not self._closed:
                    util.MONITOR.waitForAbort(0.1)

                if self.isPlayingVideo():
                    util.DEBUG_LOG('Monitoring video...')
                    self._videoMonitor()
                elif self.isPlayingAudio():
                    util.DEBUG_LOG('Monitoring audio...')
                    self._audioMonitor()
                elif self.isPlaying():
                    util.DEBUG_LOG('Monitoring pre-play...')

                    # note: this might never be triggered depending on how fast the video playback starts.
                    # don't rely on it in any way.
                    self._preplayMonitor()

            self.handler.close()
            self.close()
            util.DEBUG_LOG('Player: Closed')
        finally:
            self.trigger('session.ended')

    def _preplayMonitor(self):
        self.onPrePlayStarted()
        while self.isPlaying() and not self.isPlayingVideo() and not self.isPlayingAudio() and not util.MONITOR.abortRequested() and not self._closed:
            util.MONITOR.waitForAbort(0.1)

        if not self.isPlayingVideo() and not self.isPlayingAudio():
            self.onPlayBackFailed()

    def _videoMonitor(self):
        hasFullScreened = False

        ct = 0
        while self.isPlayingVideo() and not util.MONITOR.abortRequested() and not self._closed:
            try:
                self.currentTime = self.getTime()
            except RuntimeError:
                break

            util.MONITOR.waitForAbort(0.1)
            if xbmc.getCondVisibility('Window.IsActive(videoosd)'):
                if not self.hasOSD:
                    self.hasOSD = True
                    self.onVideoOSD()
            else:
                self.hasOSD = False

            if xbmc.getCondVisibility('Window.IsActive(seekbar)'):
                if not self.hasSeekOSD:
                    self.hasSeekOSD = True
                    self.onSeekOSD()
            else:
                self.hasSeekOSD = False

            if xbmc.getCondVisibility('VideoPlayer.IsFullscreen'):
                if not hasFullScreened:
                    hasFullScreened = True
                    self.onVideoWindowOpened()
            elif hasFullScreened and not xbmc.getCondVisibility('Window.IsVisible(busydialog)'):
                hasFullScreened = False
                self.onVideoWindowClosed()

            ct += 1
            if ct > 9:
                ct = 0
                self.handler.tick()

        if hasFullScreened:
            self.onVideoWindowClosed()

    def _audioMonitor(self):
        self.started = True
        self.handler.onMonitorInit()
        ct = 0
        while self.isPlayingAudio() and not util.MONITOR.abortRequested() and not self._closed:
            try:
                self.currentTime = self.getTime()
            except RuntimeError:
                break

            util.MONITOR.waitForAbort(0.1)

            ct += 1
            if ct > 9:
                ct = 0
                self.handler.tick()


class ZidooPlayerHandler(BasePlayerHandler):
    MODE_ABSOLUTE = 0
    MODE_RELATIVE = 1

    def __init__(self, player, session_id=None):
        BasePlayerHandler.__init__(self, player, session_id)
        self.playlist = None
        self.playQueue = None
        self.timelineType = 'video'
        self.ended = False
        self.bifURL = ''
        self.title = ''
        self.title2 = ''
        self.reset()

    def reset(self):
        self.duration = 0
        self.baseOffset = 0
        self.seekOnStart = 0
        self.mode = self.MODE_ABSOLUTE
        self.ended = False

    def setup(self, duration, meta, offset, bif_url, title='', title2='', chapters=None):
        self.ended = False
        self.baseOffset = offset / 1000.0
        self.duration = duration
        self.bifURL = bif_url
        self.title = title
        self.title2 = title2
        self.chapters = chapters or []
        self.playedThreshold = plexapp.util.INTERFACE.getPlayedThresholdValue()

    @property
    def isTranscoded(self):
        return self.mode == self.MODE_RELATIVE

    @property
    def isDirectPlay(self):
        return self.mode == self.MODE_ABSOLUTE

    @property
    def trueTime(self):
        return self.player.currentTime + self.player.playerObject.startOffset

    def shouldShowPostPlay(self):
        if self.playlist and (self.playlist.TYPE == 'playlist' or self.playlist.TYPE == 'playqueue'):
            return False

        if (not util.advancedSettings.postplayAlways and self.player.video.duration.asInt() <= FIVE_MINUTES_MILLIS) \
                or util.advancedSettings.postplayTimeout <= 0:
            return False

        return True

    def showPostPlay(self):
        if not self.shouldShowPostPlay():
            util.DEBUG_LOG("ZidooHandler: Not showing post-play")
            return False
        util.DEBUG_LOG("ZidooHandler: Showing post-play")

        if self.player.zidooFailureDialog:
            self.player.zidooFailureDialog.doClose()

        self.player.trigger('post.play', video=self.player.video, playlist=self.playlist, handler=self)

        return True

    def next(self, on_end=False):
        if self.playlist:
            if not next(self.playlist):
                return False

        if on_end:
            if self.showPostPlay():
                return True

        if not self.playlist:
            return False

        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def prev(self):
        if not self.playlist or not self.playlist.prev():
            return False

        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def playAt(self, pos):
        if not self.playlist or not self.playlist.setCurrent(pos):
            return False

        self.player.playVideoPlaylist(self.playlist, handler=self, resume=self.player.resume)

        return True

    def onPlayBackStarted(self):
        util.DEBUG_LOG(f'ZidooHandler: onPlayBackStarted, DP: {self.isDirectPlay}')

    def onAVStarted(self):
        util.DEBUG_LOG('ZidooHandler: onAVStarted')

    def onPlayBackResumed(self):
        self.updateNowPlaying()

    def onPlayBackStopped(self):
        util.DEBUG_LOG('ZidooHandler: onPlayBackStopped')
        self.updateNowPlaying()

        # show post play if possible, if an item has been watched (90% by Plex standards)
        if self.trueTime * 1000 / float(self.duration) >= self.playedThreshold:
            if self.next(on_end=True):
                return

        self.sessionEnded()

    def onPlayBackPaused(self):
        self.updateNowPlaying()

    def onPlayBackFailed(self):
        if self.ended:
            return False

        util.DEBUG_LOG('ZidooHandler: onPlayBackFailed')

        self.sessionEnded()

        return True

    def sessionEnded(self):
        if self.ended:
            return
        self.ended = True
        util.DEBUG_LOG('ZidooHandler: sessionEnded')
        time.sleep(.5) # Give the Plex server some time to update before we start querying it
        self.player.trigger('session.ended', session_id=self.sessionID)

    __next__ = next

FINAL_MARKER_NEGOFF = 1000
MARKER_SHOW_NEGOFF = 3000

class ZidooPlayer(xbmc.Player, signalsmixin.SignalsMixin):
    STATE_STOPPED = "stopped"
    STATE_PLAYING = "playing"
    STATE_PAUSED = "paused"
    STATE_BUFFERING = "buffering"

    OFFSET_RE = re.compile(r'(offset=)\d+')

    reserved_chars = '''?&|!{}[]()^~*:\\"'+-@#_.,% '''
    replace = ['\\' + l for l in reserved_chars]
    escape_table = str.maketrans(dict(zip(reserved_chars, replace)))

    def __init__(self, *args, **kwargs):
        xbmc.Player.__init__(self, *args, **kwargs)
        signalsmixin.SignalsMixin.__init__(self)
        self.handler = None  # Need to set this because creating the AudioPlayerHandler will call functions that check the handler
        self.handler = AudioPlayerHandler(self)

    def init(self):
        self._closed = False
        self._nextItem = None
        self.started = False
        self.bgmPlaying = False
        self.lastPlayWasBGM = False
        self.BGMTask = None
        self.video = None
        self.handler = AudioPlayerHandler(self)
        self.playerObject = None
        self.currentTime = 0
        self.duration = 0
        self.thread = None
        self.playState = self.STATE_STOPPED
        self.resume = False
        self.currentMarker = None
        self.zidooFailureDialog = None
        self.stopPlaybackOnIdle = util.getSetting('player_stop_on_idle', 0)
        self.idleTime = None
        self.skipNextStopNotification = False
        self.reset()
        self.open()

        return self

    def open(self):
        self._closed = False
        self.bingeMode = False
        self.autoSkipIntro = False
        self.autoSkipCredits = False
        self.autoSkipOffset = int(util.advancedSettings.autoSkipOffset * 1000)
        self.hasPlexPass = plexapp.ACCOUNT and plexapp.ACCOUNT.hasPlexPass() or False
        self.monitor()

    def close(self, shutdown=False):
        self._closed = True

    def reset(self):
        self.video = None
        self.started = False
        self.bgmPlaying = False
        self.playerObject = None
        #self.handler = AudioPlayerHandler(self)
        self.currentTime = 0
        self.duration = 0
        self.playState = self.STATE_STOPPED
        self.zidooFailureDialog = None
        self.currentMarker = None
        self.resume = False
        self.idleTime = None
        self.skipNextStopNotification = False

    def currentTrack(self):
        if self.handler.media and self.handler.media.type == 'track':
            return self.handler.media
        return None

    def play(self, *args, **kwargs):
        self.started = False

        if self.handler and isinstance(self.handler, ZidooPlayerHandler):
            url = args[0]

            #cmds = f'/system/bin/am start --user 0 -n com.hpn789.plextozidoo/.Play --ez zdmc true'
            #cmds = f'/system/bin/am start --user 0 -n com.android.gallery3d/com.android.gallery3d.app.MovieActivity'
            #audioTrack = self.video.selectedAudioStream()
            #if audioTrack:
            #    cmds += f' --ei audio_idx {audioTrack.typeIndex}'
            #subtitleTrack = self.video.selectedSubtitleStream()
            #if subtitleTrack:
            #    cmds += f' --ei subtitle_idx {subtitleTrack.typeIndex+1}' # subtitle tracks are 1 based in the zidoo player
            #cmds += f' -a android.intent.action.VIEW -t video/* --ez from_start false --ei position {self.handler.seekOnStart} -e title {self.video.title.translate(self.escape_table)} -d {url.translate(self.escape_table)}'
            #util.DEBUG_LOG(f'ZidooPlayer Cmd: {cmds}')
            # Unfortunately this always gives a security error about the shell not being owned by the uid.  Not sure why that is because I can run this just fine from termux
            #import subprocess
            #output = subprocess.run(cmds, shell=True, capture_output=True)
            #util.DEBUG_LOG(f'ZidooPlayer Output: {output}')

            # Unfortunately I can't get the "extras" to show up on the other side.  Not sure if this is a Kodi issue or something I'm doing wrong but looks like we'll always
            # need PlexToZidoo :(
            #xbmc.executebuiltin('StartAndroidActivity(com.hpn789.plextozidoo, android.intent.action.VIEW, video/*, {0}, , "[ {{ \"key\" : \"position\", \"value\" : \"{1}\", \"type\" : \"string\" }}, {{ \"key\" : \"title\", \"value\" : \"test\", \"type\" : \"string\" }} ]", , , com.hpn789.plextozidoo.Play)'.format(url, self.handler.seekOnStart))
            #xbmc.executebuiltin('StartAndroidActivity(com.android.gallery3d, android.intent.action.VIEW, video/*, {0}, , "[ {{ \"key\" : \"position\", \"value\" : \"{1}\", \"type\" : \"string\" }}, {{ \"key\" : \"title\", \"value\" : \"test\", \"type\" : \"string\" }} ]", , , com.android.gallery3d.app.MovieActivity)'.format(url, self.handler.seekOnStart))


            url = util.addURLParams(url, {
                'PlexToZidoo-ViewOffset': self.handler.seekOnStart,
                'PlexToZidoo-Title': self.video.title
            })
            audioTrack = self.video.selectedAudioStream()
            if audioTrack:
                url = util.addURLParams(url, {'PlexToZidoo-AudioIndex': audioTrack.typeIndex})
            subtitleTrack = self.video.selectedSubtitleStream(util.getSetting("forced_subtitles_override", False))
            if subtitleTrack:
                url = util.addURLParams(url, {'PlexToZidoo-SubtitleIndex': subtitleTrack.typeIndex+1}) # subtitle tracks are 1 based in the zidoo player
            if self.video.mediaChoice.part.file:
                # Can't call util.addURLParms because it doesn't handle the special characters in the path correctly
                encodedPath = six.moves.urllib.parse.quote(self.video.mediaChoice.part.file)
                url += f'&PlexToZidoo-Path={encodedPath}'

            xbmc.executebuiltin(f'StartAndroidActivity(com.hpn789.plextozidoo, android.intent.action.VIEW, video/*, {url})')

            # Put up this error message in the background in case we can't start the zidoo player.  If we actually get the player started we'll just kill this dialog
            if not self.zidooFailureDialog or self.zidooFailureDialog.closing():
                time.sleep(2)
                from .windows import optionsdialog
                self.zidooFailureDialog = optionsdialog.create(show=True, header="Error", info="Failed to start Zidoo player", button0="OK")

            self.handler.seekOnStart = 0
            self.onPrePlayStarted()
            self.onPlayBackStarted()
            self.onAVStarted()
        else:
            xbmc.Player.play(self, *args, **kwargs)

    def playBackgroundMusic(self, source, volume, rating_key, *args, **kwargs):
        if self.isPlaying():
            if not self.lastPlayWasBGM:
                return
            else:
                # don't re-queue the currently playing theme
                if self.handler.currentlyPlaying == rating_key:
                    return
                # cancel any currently playing theme before starting the new one
                else:
                    self.stopAndWait()

        if self.BGMTask and self.BGMTask.isValid():
            self.BGMTask.cancel()

        self.started = False
        self.handler = BGMPlayerHandler(self, rating_key)

        # store current volume if it's different from the BGM volume
        curVol = self.handler.getVolume()
        if volume < curVol:
            util.setSetting('last_good_volume', curVol)

        self.lastPlayWasBGM = True

        self.handler.setVolume(volume)

        self.BGMTask = BGMPlayerTask().setup(source, self, *args, **kwargs)
        backgroundthread.BGThreader.addTask(self.BGMTask)

    def playVideo(self, video, resume=False, force_update=False, session_id=None, handler=None):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = handler if handler and isinstance(handler, ZidooPlayerHandler) \
            else ZidooPlayerHandler(self, session_id)
        self.video = video
        self.resume = resume
        self.open()
        self._playVideo(resume and video.viewOffset.asInt() or 0, force_update=force_update)

    def _playVideo(self, offset=0, force_update=False, playerObject=None):
        self.trigger('new.video', video=self.video)
        self.trigger(
            'change.background',
            url=self.video.defaultArt.asTranscodedImageURL(1920, 1080, opacity=60, background=colors.noAlpha.Background)
        )
        try:
            if not playerObject:
                self.playerObject = plexplayer.PlexPlayer(self.video, offset, forceUpdate=force_update)
                self.playerObject.build()
            self.playerObject = self.playerObject.getServerDecision()
        except plexplayer.DecisionFailure as e:
            util.showNotification(e.reason, header=util.T(32448, 'Playback Failed!'))
            return
        except:
            util.ERROR(notify=True)
            return

        meta = self.playerObject.metadata
        if meta.isTranscoded:
            self.handler.mode = self.handler.MODE_RELATIVE
        else:
            self.handler.mode = self.handler.MODE_ABSOLUTE
        url = meta.streamUrls[0]
        bifURL = self.playerObject.getBifUrl()
        util.DEBUG_LOG('Playing URL(+{1}ms): {0}'.format(plexnetUtil.cleanToken(url), offset))

        self.stopAndWait()  # Stop before setting up the handler to prevent player events from causing havoc

        self.handler.setup(self.video.duration.asInt(), meta, offset, bifURL, title=self.video.grandparentTitle, title2=self.video.title, chapters=self.video.chapters)

        if self.video.type == 'episode':
            pbs = self.video.playbackSettings
            util.DEBUG_LOG("Playback settings for {}: {}".format(self.video.ratingKey, pbs))

            self.bingeMode = pbs.binge_mode

            # don't auto skip intro when on binge mode on the first episode of a season
            firstEp = self.video.index == '1'

            if self.handler.isDirectPlay or util.getUserSetting('auto_skip_in_transcode', True):
                self.autoSkipIntro = (self.bingeMode and not firstEp) or pbs.auto_skip_intro
                self.autoSkipCredits = self.bingeMode or pbs.auto_skip_credits

        # try to get an early intro offset so we can skip it if necessary
        introOffset = None
        if not offset:
            # in case we're transcoded, instruct the marker handler to set the marker a skipped, so we don't re-skip it
            # after seeking
            for marker in self.video.markers:
                if marker.type == 'intro' and self.autoSkipIntro:
                    if int(marker.startTimeOffset) <= MARKER_SHOW_NEGOFF:
                        introOffset = math.ceil(float(marker.endTimeOffset)) + self.autoSkipOffset

                        # Make sure we don't re-trigger the same marker that way the user can seek back into the skip zone and it won't automatically jump out of it again
                        if not self.currentMarker or self.currentMarker.startTimeOffset != marker.startTimeOffset:
                            self.currentMarker = marker

                        break

        if meta.isTranscoded:
            if introOffset:
                # cheat our way into an early intro skip by modifying the offset in the stream URL
                util.DEBUG_LOG("Immediately seeking behind intro: {}".format(introOffset))
                url = self.OFFSET_RE.sub(r"\g<1>{}".format(introOffset // 1000), url)

                # probably not necessary
                meta.playStart = introOffset // 1000
        else:
            if offset:
                util.DEBUG_LOG("Using as SeekOnStart: {0}; offset: {1}".format(meta.playStart, offset))
                self.handler.seekOnStart = meta.playStart * 1000
            elif introOffset:
                util.DEBUG_LOG("Seeking behind intro after playstart: {}".format(introOffset))
                self.handler.seekOnStart = introOffset

        url = util.addURLParams(url, {
            'X-Plex-Client-Profile-Name': 'Generic',
            'X-Plex-Client-Identifier': plexapp.util.INTERFACE.getGlobal('clientIdentifier')
        })



        self.play(url)

    def playVideoPlaylist(self, playlist, resume=False, handler=None, session_id=None):
        if self.bgmPlaying:
            self.stopAndWait()

        if handler and isinstance(handler, ZidooPlayerHandler):
            self.handler = handler
        else:
            self.handler = ZidooPlayerHandler(self, session_id)

        self.handler.playlist = playlist
        if playlist.isRemote:
            self.handler.playQueue = playlist
        self.video = playlist.current()
        self.video.softReload()
        self.resume = resume
        self.currentTime = 0
        self.open()
        self._playVideo(resume and self.video.viewOffset.asInt() or 0, force_update=True)

    def playAudio(self, track, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        url, li = self.createTrackListItem(track, fanart)
        self.stopAndWait()
        self.play(url, li, **kwargs)

    def playAlbum(self, album, startpos=-1, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        plist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        plist.clear()
        index = 1
        for track in album.tracks():
            url, li = self.createTrackListItem(track, fanart, index=index)
            plist.add(url, li)
            index += 1
        xbmc.executebuiltin('PlayerControl(RandomOff)')
        self.stopAndWait()
        self.play(plist, startpos=startpos, **kwargs)

    def playAudioPlaylist(self, playlist, startpos=-1, fanart=None, **kwargs):
        if self.bgmPlaying:
            self.stopAndWait()

        self.handler = AudioPlayerHandler(self)
        plist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        plist.clear()
        index = 1
        for track in playlist.items():
            url, li = self.createTrackListItem(track, fanart, index=index)
            plist.add(url, li)
            index += 1

        if playlist.isRemote:
            self.handler.setPlayQueue(playlist)
        else:
            if playlist.startShuffled:
                plist.shuffle()
                xbmc.executebuiltin('PlayerControl(RandomOn)')
            else:
                xbmc.executebuiltin('PlayerControl(RandomOff)')
        self.stopAndWait()
        self.play(plist, startpos=startpos, **kwargs)

    def createTrackListItem(self, track, fanart=None, index=0):
        data = base64.urlsafe_b64encode(track.serialize().encode("utf8")).decode("utf8")
        url = 'plugin://script.zidooplexmod/play?{0}'.format(data)
        li = xbmcgui.ListItem(track.title, path=url)
        if float(xbmc.getInfoLabel('System.BuildVersionShort')) < 20.0:
            li.setInfo('music', {
                'artist': six.text_type(track.originalTitle or track.grandparentTitle),
                'title': six.text_type(track.title),
                'album': six.text_type(track.parentTitle),
                'discnumber': track.parentIndex.asInt(),
                'tracknumber': track.get('index').asInt(),
                'duration': int(track.duration.asInt() / 1000),
                'playcount': index,
                'comment': 'PLEX-{0}:{1}'.format(track.ratingKey, data)
            })
        else:
            minfo = li.getMusicInfoTag()
            minfo.setArtist(six.text_type(track.originalTitle or track.grandparentTitle))
            minfo.setTitle(six.text_type(track.title))
            minfo.setAlbum(six.text_type(track.parentTitle))
            minfo.setDisc(track.parentIndex.asInt())
            minfo.setTrack(track.get('index').asInt())
            minfo.setDuration(int(track.duration.asInt() / 1000))
            minfo.setPlayCount(index)
            minfo.setComment('PLEX-{0}:{1}'.format(track.ratingKey, data))
        art = fanart or track.defaultArt
        li.setArt({
            'fanart': art.asTranscodedImageURL(1920, 1080),
            'landscape': util.backgroundFromArt(art),
            'thumb': track.defaultThumb.asTranscodedImageURL(800, 800),
        })
        if fanart:
            li.setArt({'fanart': fanart})
        return (url, li)

    def onPrePlayStarted(self):
        util.DEBUG_LOG('ZidooPlayer: PRE-PLAY')
        self.trigger('preplay.started')
        if not self.handler:
            return
        self.handler.onPrePlayStarted()

    def onPlayBackStarted(self):
        util.DEBUG_LOG('ZidooPlayer: STARTED')
        self.trigger('playback.started')
        self.started = True
        self.currentTime = .001 # Need to trick the timeline update flows otherwise they ignore a 0 time
        if not self.handler:
            return
        self.handler.onPlayBackStarted()

    def onAVStarted(self):
        util.DEBUG_LOG('ZidooPlayer: AVStarted - {}'.format(self.handler))
        self.trigger('av.started')
        if not self.handler:
            return
        self.handler.onAVStarted()

    def onPlayBackPaused(self):
        util.DEBUG_LOG('ZidooPlayer: PAUSED')
        if not self.handler:
            return
        self.handler.onPlayBackPaused()

    def onPlayBackResumed(self):
        util.DEBUG_LOG('ZidooPlayer: RESUMED')
        if not self.handler:
            return

        self.handler.onPlayBackResumed()

    def onPlayBackStopped(self):
        if self.skipNextStopNotification:
            util.DEBUG_LOG('ZidooPlayer: SKIP')
            self.skipNextStopNotification = False
            return

        if not self.started:
            self.onPlayBackFailed()

        if self.lastPlayWasBGM and not self.isPlaying():
            util.DEBUG_LOG('ZidooPlayer: STOP BGM')
            self.lastPlayWasBGM = False
            self.skipNextStopNotification = True

        util.DEBUG_LOG('ZidooPlayer: STOPPED' + (not self.started and ': FAILED' or ''))
        self.started = False
        if not self.handler:
            return
        self.handler.onPlayBackStopped()

    def onPlayBackEnded(self):
        if not self.started:
            self.onPlayBackFailed()

        self.lastPlayWasBGM = False

        util.DEBUG_LOG('ZidooPlayer: ENDED' + (not self.started and ': FAILED' or ''))
        self.started = False
        if not self.handler:
            return
        self.handler.onPlayBackEnded()

    def onPlayBackFailed(self):
        util.DEBUG_LOG('ZidooPlayer: FAILED - {}'.format(self.handler))
        if not self.handler:
            return

        if self.handler.onPlayBackFailed():
            util.showNotification(util.T(32448, 'Playback Failed!'))
            self.stopAndWait()
            self.close()

    def stopAndWait(self):
        if self.isPlaying():
            util.DEBUG_LOG('ZidooPlayer: Stopping and waiting...')
            self.stop()
            while not util.MONITOR.abortRequested() and self.isPlaying():
                time.sleep(0.1)
            time.sleep(0.2)
            if isinstance(self.handler, BGMPlayerHandler):
                self.onPlayBackStopped()
            util.DEBUG_LOG('ZidooPlayer: Stopping and waiting...Done')

    def monitor(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._monitor, name='PLAYER:MONITOR')
            self.thread.start()

    def _monitor(self):
        try:
            while not util.MONITOR.abortRequested() and not self._closed:
                util.DEBUG_LOG('ZidooPlayer: Monitor 0')
                if not self.started:
                    util.DEBUG_LOG('ZidooPlayer: Idling...')

                # Wait for something to start
                while (not self.started or not self.handler or not isinstance(self.handler, ZidooPlayerHandler)) and not util.MONITOR.abortRequested() and not self._closed:
                    time.sleep(1)

                util.DEBUG_LOG('ZidooPlayer: Monitor 1')
                # Wait for the zidoo player to get going
                zidooStatusFull = None
                while((zidooStatusFull is None or zidooStatusFull['video']['duration'] <= 0) and not util.MONITOR.abortRequested() and not self._closed):
                    time.sleep(1)
                    zidooStatusFull = self.getZidooPlayerStatus()
                    if zidooStatusFull is None:
                        # Check to see if the user cleared the error message, if so then we can stop monitoring
                        if self.zidooFailureDialog is None or self.zidooFailureDialog.closing():
                            self.playState = self.STATE_STOPPED
                            break

                if zidooStatusFull is not None:
                    util.DEBUG_LOG('ZidooPlayer: Monitor 2')
                    # Loop here while the movie is still being played
                    while self.started and not util.MONITOR.abortRequested() and not self._closed:
                        time.sleep(1)
                        timeJump = False
                        zidooStatusFull = self.getZidooPlayerStatus()
                        if zidooStatusFull is not None:
                            if zidooStatusFull['video']['duration'] > 0:
                                zidooStatus = zidooStatusFull['video']['status']
                                if zidooStatus == 0 or zidooStatus == 1:
                                    if zidooStatus == 0:
                                        if self.playState != self.STATE_PAUSED:
                                            self.playState = self.STATE_PAUSED
                                            self.onPlayBackPaused()
                                            self.idleTime = time.time()
                                            continue # Loop back to the top so we give the Plex server a chance to catch up
                                        if self.stopPlaybackOnIdle:
                                            if self.idleTime and time.time() - self.idleTime >= self.stopPlaybackOnIdle:
                                                util.DEBUG_LOG('ZidooPlayer: Monitor idle time expired - stopping playback')
                                                self.setZidooPlayerStop()
                                                continue
                                    elif zidooStatus == 1:
                                        if self.playState != self.STATE_PLAYING:
                                            self.playState = self.STATE_PLAYING
                                            self.onPlayBackResumed()
                                            self.idleTime = None
                                            continue # Loop back to the top so we give the Plex server a chance to catch up
                                    self.duration = zidooStatusFull['video']['duration'] / 1000
                                    newTime = zidooStatusFull['video']['currentPosition']
                                    if newTime > 0:
                                        # If the time change since the last update is more than 10 seconds we want to
                                        # update the plex server
                                        if abs(newTime - (self.currentTime * 1000)) > 10000:
                                            timeJump = True

                                        self.currentTime = newTime / 1000

                                        if self.autoSkipIntro or self.autoSkipCredits:
                                            self.checkAutoSkip()
                                else:
                                    self.playState = self.STATE_STOPPED
                                    break
                            else:
                                self.playState = self.STATE_STOPPED
                                break
                        else:
                            # Check to see if the user cleared the error message, if so then we can stop monitoring
                            if self.zidooFailureDialog is None or self.zidooFailureDialog.closing():
                                self.playState = self.STATE_STOPPED
                                break

                        if timeJump:
                            self.handler.updateNowPlaying(force=True, state=self.STATE_PAUSED) # The PAUSED state should actually force an update
                        else:
                            self.handler.updateNowPlaying(force=True)

                util.DEBUG_LOG('ZidooPlayer: Monitor 3')
                if not util.MONITOR.abortRequested() and not self._closed:
                    self.currentMarker = None
                    self.onPlayBackStopped()

            self.handler.close()
            self.close()
            util.DEBUG_LOG('ZidooPlayer: Closed')
        finally:
            self.trigger('session.ended')

    def getTotalTime(self):
        if not self.handler or not isinstance(self.handler, ZidooPlayerHandler):
            return super().getTotalTime()
        else:
            return self.duration

    def getTime(self):
        if not self.handler or not isinstance(self.handler, ZidooPlayerHandler):
            return super().getTime()
        else:
            return self.currentTime

    def getZidooPlayerStatus(self):
        try:
            url = 'http://127.0.0.1:9529/ZidooVideoPlay/getPlayStatus'
            response = requests.get(url, timeout=2)
        except requests.exceptions.RequestException as e:
            util.ERROR('Zidoo player status failed')
            return None

        response_json = response.json()
        util.DEBUG_LOG(response_json)
        if response_json['status'] != 200:
            return None

        return response_json

    def setZidooPlayerSeek(self, position):
        try:
            url = f'http://127.0.0.1:9529/ZidooVideoPlay/seekTo?positon={position}'
            response = requests.get(url, timeout=2)
        except requests.exceptions.RequestException as e:
            util.ERROR('Zidoo player seek failed')
            return None

        response_json = response.json()
        util.DEBUG_LOG(response_json)
        if response_json['status'] != 200:
            return None

        return response_json

    def setZidooPlayerStop(self):
        try:
            url = 'http://127.0.0.1:9529/ZidooControlCenter/RemoteControl/sendkey?key=Key.MediaStop'
            response = requests.get(url, timeout=2)
        except requests.exceptions.RequestException as e:
            util.ERROR('Zidoo player stop failed')
            return None

        response_json = response.json()
        util.DEBUG_LOG(response_json)
        if response_json['status'] != 200:
            return None

        return response_json

    def checkAutoSkip(self):
        if not self.hasPlexPass or not self.video.markers:
            return

        for marker in self.video.markers:
            if (marker.type == 'intro' and self.autoSkipIntro) or (marker.type == 'credits' and self.autoSkipCredits):
                # Make sure we don't use any negative time values
                triggerStartTime = int(marker.startTimeOffset) + self.autoSkipOffset
                if triggerStartTime < 0:
                    triggerStartTime = 0

                # Make sure we don't skip past the end, the FINAL_MARKER_NEGOFF is so that the postplay screen will show
                triggerEndTime = math.ceil(float(marker.endTimeOffset)) + self.autoSkipOffset
                if triggerEndTime > (int(self.getTotalTime() * 1000) - FINAL_MARKER_NEGOFF):
                    triggerEndTime = int(self.getTotalTime() * 1000) - FINAL_MARKER_NEGOFF

                if triggerStartTime <= math.floor(self.currentTime*1000) < triggerEndTime:
                    # Make sure we don't re-trigger the same marker that way the user can seek back into the skip zone and it won't automatically jump out of it again
                    if not self.currentMarker or self.currentMarker.startTimeOffset != marker.startTimeOffset:
                        self.currentMarker = marker
                        util.DEBUG_LOG(f'ZidooAutoSkip: Skipping to {triggerEndTime}')
                        self.setZidooPlayerSeek(triggerEndTime)
                    break

    def isPlaying(self):
        if not self.handler or not isinstance(self.handler, ZidooPlayerHandler):
            return super().isPlaying()
        else:
            return self.playState != self.STATE_STOPPED

    def isPlayingAudio(self):
        if not self.handler or not isinstance(self.handler, ZidooPlayerHandler):
            return super().isPlayingAudio()
        else:
            return False

    def stop(self):
        if not self.handler or not isinstance(self.handler, ZidooPlayerHandler):
            super().stop()

def shutdown():
    global PLAYER
    PLAYER.close(shutdown=True)
    del PLAYER


PLAYER = ZidooPlayer().init()
