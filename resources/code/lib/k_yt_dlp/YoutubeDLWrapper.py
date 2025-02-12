# -*- coding: utf-8 -*-
import sys
import time
import datetime
from typing import Set, Type

from kodi_six import xbmc

# Use relative import to keep Kodi repo's code checker happy, but it is
# probably better form to do it this way.

from k_yt_dlp.yd_private_libs import util, updater
from k_yt_dlp import YDStreamUtils as StreamUtils
from k_yt_dlp.strptime_patch import StripTimePatch;

# Apply datetime.strptime patch, if not already done.

StripTimePatch.monkey_patch_strptime()

updater.updateCore()

updater.set_yt_dlp_importPath()

from k_yt_dlp.yt_dlp.utils import std_headers as std_headers # noqa E402
from k_yt_dlp.yt_dlp.utils import DownloadError as DownloadError # noqa E402

DownloadError  # Hides IDE warnings


###############################################################################
# FIX: xbmcout instance in sys.stderr does not have isatty(), so we add it
###############################################################################


class replacement_stderr(sys.stderr.__class__):
    def isatty(self):
        return False


sys.stderr.__class__ = replacement_stderr

###############################################################################
# FIX: _subprocess doesn't exist on Xbox One
###############################################################################

try:
    import _subprocess
except ImportError:
    from k_yt_dlp.yd_private_libs import _subprocess

###############################################################################

try:
    from k_yt_dlp import yt_dlp as yt_lib
except Exception:
    util.ERROR('Failed to import youtube-dl')
    yt_lib = None

coreVersion = yt_lib.version.__version__
updater.saveVersion(coreVersion)
util.LOG('yt_lib core version: {0}'.format(coreVersion))

from k_yt_dlp.yt_dlp import YoutubeDL as YoutubeDL

_YTDL = None
_DISABLE_DASH_VIDEO = util.getSetting('disable_dash_video', True)
_CALLBACK = None
# BLACKLIST = ['youtube:playlist', 'youtube:toplist', 'youtube:channel', 'youtube:user', 'youtube:search', 'youtube:show', 'youtube:favorites', 'youtube:truncated_url','vimeo:channel', 'vimeo:user', 'vimeo:album', 'vimeo:group', 'vimeo:review','dailymotion:playlist', 'dailymotion:user','generic'] # noqa E501
_BLACKLIST = []
_OVERRIDE_PARAMS = {}
_DOWNLOAD_CANCEL = False
_DOWNLOAD_START = None
_DOWNLOAD_DURATION = None


class VideoInfo:
    """
    Represents resolved site video
    Has the properties title, description, thumbnail and webpage
    The info property contains the original yt-dlp info
    """

    def __init__(self, ID=None):
        self.ID = ID
        self.title = ''
        self.description = ''
        self.thumbnail = ''
        self.webpage = ''
        self._streams = None
        self.sourceName = ''
        self.info = None
        self._selection = None
        self.downloadID = str(time.time())

    def __len__(self):
        return len(self._streams)

    def streamURL(self):
        """
        Returns the resolved xbmc ready url of the selected stream
        """
        return self.selectedStream()['xbmc_url']

    def streams(self):
        """
        Returns a list of dicts of stream data:
            {'xbmc_url':<xbmc ready resolved stream url>,
            'url':<base resolved stream url>,
            'title':<stream specific title>,
            'thumbnail':<stream specific thumbnail>,
            'formatID':<chosen format id>}
        """
        return self._streams

    def hasMultipleStreams(self):
        """
        Return True if there is more than one stream
        """
        if not self._streams:
            return False
        if len(self._streams) > 1:
            return True
        return False

    def selectStream(self, idx):
        """
        Select the default stream by index or by passing the stream dict
        """
        if isinstance(idx, dict):
            self._selection = idx['idx']
        else:
            self._selection = idx

    def selectedStream(self):
        """
        Returns the info of the currently selected stream
        """
        if self._selection is None:
            return self._streams[0]
        return self._streams[self._selection]


class DownloadCanceledException(Exception):
    pass


class CallbackMessage(str):
    """
    A callback message. Subclass of string so can be displayed/printed as is.
    Has the following extra properties:
        percent        <- Integer download progress or 0 if not available
        etaStr        <- ETA string ex: 3m 25s
        speedStr    <- Speed string ex: 35 KBs
        info        <- dict of the yt-dlp progress info
    """

    def __new__(self, value, pct=0, eta_str='', speed_str='', info=None):
        return str.__new__(self, value)

    def __init__(self, value, pct=0, eta_str='', speed_str='', info=None):
        self.percent = pct
        self.etaStr = eta_str
        self.speedStr = speed_str
        self.info = info


class YoutubeDLWrapper(YoutubeDL):
    """
    A wrapper for yt-dlp.YoutubeDL providing message handling and
    progress callback.
    It also overrides XBMC environment error causing methods.
    """

    def __init__(self, *args, **kwargs):
        self._lastDownloadedFilePath = ''
        self._overrideParams = {}
        self._monitor = xbmc.Monitor()
        self._printed_messages: Type[set[str]] = set()

        YoutubeDL.__init__(self, *args, **kwargs)

    def showMessage(self, msg):
        global _CALLBACK
        if _CALLBACK:
            try:
                return _CALLBACK(msg)
            except Exception:
                util.ERROR('Error in callback. Removing.')
                _CALLBACK = None
        else:
            if self._monitor.abortRequested():
                raise Exception('abortRequested')
            # print msg.encode('ascii','replace')
        return True

    def progressCallback(self, info):
        global _DOWNLOAD_CANCEL
        if self._monitor.abortRequested() or _DOWNLOAD_CANCEL:
            _DOWNLOAD_CANCEL = False
            raise DownloadCanceledException('abortRequested')
        if _DOWNLOAD_DURATION:
            if time.time() - _DOWNLOAD_START > _DOWNLOAD_DURATION:
                raise DownloadCanceledException('duration_reached')
        if not _CALLBACK:
            return
        # 'downloaded_bytes': byte_counter,
        # 'total_bytes': data_len,
        # 'tmpfilename': tmpfilename,
        # 'filename': filename,
        # 'status': 'downloading',
        # 'eta': eta,
        # 'speed': speed
        sofar = info.get('downloaded_bytes')
        total = info.get('total_bytes') or info.get('total_bytes_estimate')
        if info.get('filename'):
            self._lastDownloadedFilePath = info.get('filename')
        pct = ''
        pct_val = 0
        eta = None
        if sofar is not None and total:
            pct_val = int((float(sofar) / total) * 100)
            pct = ' (%s%%)' % pct_val
        elif _DOWNLOAD_DURATION:
            sofar = time.time() - _DOWNLOAD_START
            eta = _DOWNLOAD_DURATION - sofar
            pct_val = int((float(sofar) / _DOWNLOAD_DURATION) * 100)
        eta = eta or info.get('eta') or ''
        eta_str = ''
        if eta:
            eta_str = StreamUtils.durationToShortText(eta)
            eta = '  ETA: ' + eta_str
        speed = info.get('speed') or ''
        speed_str = ''
        if speed:
            speed_str = StreamUtils.simpleSize(speed) + 's'
            speed = '  ' + speed_str
        status = '%s%s:' % (info.get('status', '?').title(), pct)
        text = CallbackMessage(status + eta + speed, pct_val, eta_str, speed_str, info)
        ok = self.showMessage(text)
        if not ok:
            util.LOG('Download canceled')
            raise DownloadCanceledException()

    def clearDownloadParams(self):
        self.params['quiet'] = False
        self.params['format'] = None
        self.params['matchtitle'] = None
        self.params.update(_OVERRIDE_PARAMS)

    def clear_progress_hooks(self):
        self._progress_hooks = []

    def add_info_extractor(self, ie):
        if ie.IE_NAME in _BLACKLIST:
            return
        # Fix ##################################################################
        # module = sys.modules.get(ie.__module__)
        # if module:
        #     if hasattr(module, 'unified_strdate'):
        #         module.unified_strdate = _unified_strdate_wrap
        #     if hasattr(module, 'date_from_str'):
        #         module.date_from_str = _date_from_str_wrap
        ########################################################################
        YoutubeDL.add_info_extractor(self, ie)

    def to_stdout(self, message, skip_eol=False, check_quiet=False):
        """Print message to stdout if not in quiet mode."""
        if self.params.get('logger'):
            self.params['logger'].debug(message)
        elif not check_quiet or not self.params.get('quiet', False):
            message = self._bidi_workaround(message)
            terminator = ['\n', ''][skip_eol]
            output = message + terminator
            self.showMessage(output)

    def to_stderr(self, message: str, only_once: bool = False):
        """Print message to stderr."""
        assert isinstance(message, str)
        if only_once:
            if message in self._printed_messages:
                return
            self._printed_messages.add(message)

        if self.params.get('logger'):
            self.params['logger'].error(message)
        else:
            message = self._bidi_workaround(message)
            output = message + '\n'
            self.showMessage(output)

    def report_warning(self, message: str, only_once: bool = False):
        # overidden to get around error on missing stderr.isatty attribute
        _msg_header = 'WARNING:'
        warning_message = '%s %s' % (_msg_header, message)
        self.to_stderr(warning_message, only_once)

    def report_error(self, message, *args, **kwargs):
        # overidden to get around error on missing stderr.isatty attribute
        _msg_header = 'ERROR:'
        error_message = '%s %s' % (_msg_header, message)
        self.trouble(error_message, *args, **kwargs)


def _getYTDL():
    global _YTDL
    if _YTDL:
        return _YTDL
    if util.DEBUG and util.getSetting('ytdl_debug', False):
        _YTDL = YoutubeDLWrapper({'verbose': True})
    else:
        _YTDL = YoutubeDLWrapper()
    _YTDL.add_progress_hook(_YTDL.progressCallback)
    _YTDL.add_default_info_extractors()
    return _YTDL


def download(info):
    from k_yt_dlp.yt_dlp import downloader
    ytdl = _getYTDL()
    if 'http_headers' not in info:
        info['http_headers'] = std_headers
    return ytdl.process_info(info)
