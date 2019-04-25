import sys
import os.path
import vlc

import subprocess
from PyQt4 import QtGui, QtCore
import wave
import argparse
import audioop
from googleapiclient.discovery import build
import json
import math

# from multiprocessing import Process, freeze_support

import multiprocessing
import os
import requests
import subprocess
import tempfile
import wave

from progressbar import ProgressBar, Percentage, Bar, ETA

from autosub.constants import LANGUAGE_CODES, \
    GOOGLE_SPEECH_API_KEY, GOOGLE_SPEECH_API_URL
from autosub.formatters import FORMATTERS

def percentile(arr, percent):
    arr = sorted(arr)
    k = (len(arr) - 1) * percent
    f = math.floor(k)
    c = math.ceil(k)
    if f == c: return arr[int(k)]
    d0 = arr[int(f)] * (c - k)
    d1 = arr[int(c)] * (k - f)
    return d0 + d1


def is_same_language(lang1, lang2):
    return lang1.split("-")[0] == lang2.split("-")[0]


class FLACConverter(object):
    def __init__(self, source_path, include_before=0.25, include_after=0.25):
        self.source_path = source_path
        self.include_before = include_before
        self.include_after = include_after

    def __call__(self, region):
        try:
            start, end = region
            start = max(0, start - self.include_before)
            end += self.include_after
            temp = tempfile.NamedTemporaryFile(suffix='.flac', delete=False)
            command = ["ffmpeg","-ss", str(start), "-t", str(end - start),
                       "-y", "-i", self.source_path,
                       "-loglevel", "error", temp.name]
            subprocess.check_output(command, stdin=open(os.devnull))
            return temp.read()

        except KeyboardInterrupt:
            return


class SpeechRecognizer(object):
    def __init__(self, language="en", rate=44100, retries=3, api_key=GOOGLE_SPEECH_API_KEY):
        self.language = language
        self.rate = rate
        self.api_key = api_key
        self.retries = retries

    def __call__(self, data):
        try:
            for i in range(self.retries):
                url = GOOGLE_SPEECH_API_URL.format(lang=self.language, key=self.api_key)
                headers = {"Content-Type": "audio/x-flac; rate=%d" % self.rate}

                try:
                    resp = requests.post(url, data=data, headers=headers)
                except requests.exceptions.ConnectionError:
                    continue

                for line in resp.content.split("\n"):
                    try:
                        line = json.loads(line)
                        line = line['result'][0]['alternative'][0]['transcript']
                        return line[:1].upper() + line[1:]
                    except:
                        # no result
                        continue

        except KeyboardInterrupt:
            return


class Translator(object):
    def __init__(self, language, api_key, src, dst):
        self.language = language
        self.api_key = api_key
        self.service = build('translate', 'v2',
                             developerKey=self.api_key)
        self.src = src
        self.dst = dst

    def __call__(self, sentence):
        try:
            if not sentence: return
            result = self.service.translations().list(
                source=self.src,
                target=self.dst,
                q=[sentence]
            ).execute()
            if 'translations' in result and len(result['translations']) and \
                            'translatedText' in result['translations'][0]:
                return result['translations'][0]['translatedText']
            return ""

        except KeyboardInterrupt:
            return


def which(program):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path,  program + ".exe")
            if is_exe(exe_file):
                return exe_file
    return None


def extract_audio(filename, channels=1, rate=16000):
    temp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    if not os.path.isfile(filename):
        print "The given file does not exist: {0}".format(filename)
        raise Exception("Invalid filepath: {0}".format(filename))
    if not which("ffmpeg"):
        print "ffmpeg: Executable not found on machine."
        raise Exception("Dependency not found: ffmpeg")
    command = ["ffmpeg", "-y", "-i", filename, "-ac", str(channels), "-ar", str(rate), "-loglevel", "error", temp.name]
    subprocess.check_output(command, stdin=open(os.devnull))
    return temp.name, rate


def find_speech_regions(filename, frame_width=4096, min_region_size=0.5, max_region_size=6):
    reader = wave.open(filename)
    sample_width = reader.getsampwidth()
    rate = reader.getframerate()
    n_channels = reader.getnchannels()

    total_duration = reader.getnframes() / rate
    chunk_duration = float(frame_width) / rate

    n_chunks = int(total_duration / chunk_duration)
    energies = []

    for i in range(n_chunks):
        chunk = reader.readframes(frame_width)
        energies.append(audioop.rms(chunk, sample_width * n_channels))

    threshold = percentile(energies, 0.2)

    elapsed_time = 0

    regions = []
    region_start = None

    for energy in energies:
        is_silence = energy <= threshold
        max_exceeded = region_start and elapsed_time - region_start >= max_region_size

        if (max_exceeded or is_silence) and region_start:
            if elapsed_time - region_start >= min_region_size:
                regions.append((region_start, elapsed_time))
                region_start = None

        elif (not region_start) and (not is_silence):
            region_start = elapsed_time
        elapsed_time += chunk_duration
    return regions


def get_duration_wav(wav_filename):
    f = wave.open(wav_filename, 'r')
    frames = f.getnframes()
    rate = f.getframerate()
    duration = frames / float(rate)
    f.close()
    return duration

class Player(QtGui.QMainWindow):
    """A simple Media Player using VLC and Qt
    """
    def __init__(self, master=None):
        QtGui.QMainWindow.__init__(self, master)
        self.setWindowTitle("Media Player")
        self.setWindowIcon(QtGui.QIcon("./logo.png"))
        # creating a basic vlc instance
        self.instance = vlc.Instance()
        # creating an empty vlc media player
        self.mediaplayer = self.instance.media_player_new()
        self.duration = 0
        self.sec = -1
        self.createUI()
        self.isPaused = False

    def createUI(self):
        """Set up the user interface, signals & slots
        """
        
        self.widget = QtGui.QWidget(self)
        #self.widget.resize(100,100)
        self.setCentralWidget(self.widget)
        
        # In this widget, the video will be drawn
        if sys.platform == "darwin": # for MacOS
            self.videoframe = QtGui.QMacCocoaViewContainer(0)
        else:
            self.videoframe = QtGui.QFrame()
            
        self.palette = self.videoframe.palette()
        self.palette.setColor (QtGui.QPalette.Window,
                               QtGui.QColor(0,0,0))
        self.videoframe.setPalette(self.palette)
        self.videoframe.setAutoFillBackground(True)
        self.positionslider = QtGui.QSlider(QtCore.Qt.Horizontal, self)
        self.positionslider.setToolTip("Position")
        self.positionslider.setMaximum(1000)
        self.connect(self.positionslider,
                     QtCore.SIGNAL("sliderMoved(int)"), self.setPosition)

        self.hbuttonbox = QtGui.QHBoxLayout()
        self.playbutton = QtGui.QPushButton("Play")
        self.hbuttonbox.addWidget(self.playbutton)
        self.connect(self.playbutton, QtCore.SIGNAL("clicked()"),
                     self.PlayPause)

        self.stopbutton = QtGui.QPushButton("Stop")
        self.hbuttonbox.addWidget(self.stopbutton)
        self.connect(self.stopbutton, QtCore.SIGNAL("clicked()"),
                     self.Stop)

        self.hbuttonbox.addStretch(1)
        self.volumeslider = QtGui.QSlider(QtCore.Qt.Horizontal, self)
        self.volumeslider.setMaximum(100)
        self.volumeslider.setValue(self.mediaplayer.audio_get_volume())
        self.volumeslider.setToolTip("Volume")
        self.hbuttonbox.addWidget(self.volumeslider)
        self.connect(self.volumeslider,
                     QtCore.SIGNAL("valueChanged(int)"),
                     self.setVolume)

        self.vboxlayout = QtGui.QVBoxLayout()
        self.vboxlayout.addWidget(self.videoframe)
        self.vboxlayout.addWidget(self.positionslider)
        self.vboxlayout.addLayout(self.hbuttonbox)
        self.widget.setLayout(self.vboxlayout)

        self.prevbutton = QtGui.QPushButton("Prev")
        self.hbuttonbox.addWidget(self.prevbutton)
        self.connect(self.prevbutton, QtCore.SIGNAL("clicked()"),
                     self.prev)

        self.textbox = QtGui.QLineEdit(self)
        self.hbuttonbox.addWidget(self.textbox)
        #self.textbox.move(145, 230)
        self.textbox.resize(200,30)
        self.searchbutton = QtGui.QPushButton("search")
        self.hbuttonbox.addWidget(self.searchbutton)
        self.connect(self.searchbutton, QtCore.SIGNAL("clicked()"),
                     self.srch)

        self.nextbutton = QtGui.QPushButton("Next")
        self.hbuttonbox.addWidget(self.nextbutton)
        self.connect(self.nextbutton, QtCore.SIGNAL("clicked()"),
                     self.next)

        self.comboBox = QtGui.QComboBox(self)
        self.hbuttonbox.addWidget(self.comboBox)
        #self.connect(self.nextbutton, QtCore.SIGNAL("clicked()"),
        #             self.next)
        self.comboBox.activated[str].connect(self.srch1)
        open = QtGui.QAction("&Open", self)
        open.setShortcut("Ctrl+O")
        self.connect(open, QtCore.SIGNAL("triggered()"), self.OpenFile)
        exit = QtGui.QAction("&Exit", self)
        exit.setShortcut("Ctrl+Q")
        self.connect(exit, QtCore.SIGNAL("triggered()"), sys.exit)
        menubar = self.menuBar()
        filemenu = menubar.addMenu("&File")
        filemenu.addAction(open)
        filemenu.addSeparator()
        filemenu.addAction(exit)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.connect(self.timer, QtCore.SIGNAL("timeout()"),
                     self.updateUI)

    def srch(self):
        wrd = self.textbox.text()
        if wrd == '':
            self.err("Please Enter Word")
        else:
            # self.sec = -1
            self.x = search(str(wrd),self.sub_path)
            print self.x
            if self.x[1]:
                self.sec = 0
                self.mediaplayer.set_position(toSec(self.x[0][0].split(',')[0]) / self.duration);
            else:
                self.err("No Word Found")

    def srch1(self):
        wrd = str(self.comboBox.currentText())
        if wrd == '':
            pass
        else:
            # self.sec = -1
            self.x = search(str(wrd),self.sub_path)
            print self.x
            if self.x[1]:
                self.sec = 0
                self.textbox.setText(wrd)
                self.mediaplayer.set_position(toSec(self.x[0][0].split(',')[0]) / self.duration);
            else:
                self.err("No Word Found")


    def next(self):
        if self.sec == -1:
            pass
        else:
            if self.x[1]:
                if self.sec + 1 < len(self.x[0]):
                    self.sec += 1
                    self.mediaplayer.set_position(toSec(self.x[0][self.sec].split(',')[0]) / self.duration);

    def prev(self):
        if self.sec == -1:
            pass
        else:
            if self.x[1]:
                if self.sec > 0:
                    self.sec -= 1
                    self.mediaplayer.set_position(toSec(self.x[0][self.sec].split(',')[0]) / self.duration);

    def PlayPause(self):
        """Toggle play/pause status
        """
        if self.mediaplayer.is_playing():
            self.mediaplayer.pause()
            self.playbutton.setText("Play")
            self.isPaused = True
        else:
            if self.mediaplayer.play() == -1:
                self.OpenFile()
                return
            self.mediaplayer.play()
            self.playbutton.setText("Pause")
            self.timer.start()
            self.isPaused = False

    def Stop(self):
        """Stop player
        """
        self.mediaplayer.stop()
        self.playbutton.setText("Play")

    def Next(self):
            self.mediaplayer.set_position(57.5 / self.duration);


    def OpenFile(self, filename=None):
        """Open a media file in a MediaPlayer
        
        if filename is None:
            filename = QtGui.QFileDialog.getOpenFileName(self, "Open File", os.path.expanduser('~'))
        if not filename:
            return"""
        
        filename = QtGui.QFileDialog.getOpenFileName(self, 'Open File')
        formats = ['.mp4', '.mkv', '.avi', '.MP4', '.MKV', '.AVI']
        base, ext = os.path.splitext(str(filename))
        self.sub_path =  base + '.' + 'srt'
        if filename == "" or ext not in formats:
            self.invalid_file()
        else:
            if os.path.isfile(base + '.' + 'srt'):
                print 'subtitle file already present'
            else:
                audio_filename, audio_rate = extract_audio(str(filename))
                command="ffmpeg -i " + str(filename) + " -acodec pcm_s16le -ac 1 -ar 16000 " + base +".wav"
                print command
                subprocess.call(command, shell=True)
                regions = find_speech_regions(audio_filename)
                # freeze_support()
                pool = multiprocessing.Pool(10)
                converter = FLACConverter(source_path=audio_filename)
                recognizer = SpeechRecognizer(language="en", rate=audio_rate, api_key=GOOGLE_SPEECH_API_KEY)

                transcripts = []
                if regions:
                    try:
                        widgets = ["Converting speech regions to FLAC files: ", Percentage(), ' ', Bar(), ' ', ETA()]
                        pbar = ProgressBar(widgets=widgets, maxval=len(regions)).start()
                        # self.progressLabel.setText("Converting speech regions to FLAC files: ")
                        extracted_regions = []
                        for i, extracted_region in enumerate(pool.imap(converter, regions)):
                            extracted_regions.append(extracted_region)
                            pbar.update(i)
                            # self.progress.setValue(i * 100 / 66)
                        pbar.finish()

                        # self.progress.setValue(0)

                        widgets = ["Performing speech recognition: ", Percentage(), ' ', Bar(), ' ', ETA()]
                        pbar = ProgressBar(widgets=widgets, maxval=len(regions)).start()
                        # self.progressLabel.setText("Performing speech recognition: ")
                        for i, transcript in enumerate(pool.imap(recognizer, extracted_regions)):
                            transcripts.append(transcript)
                            pbar.update(i)
                            # self.progress.setValue(i * 100 / 39)
                        pbar.finish()
                        # self.progress.setValue(100)

                        if not is_same_language("en", "en"):
                            if args.api_key:
                                google_translate_api_key = self.args.api_key
                                translator = Translator(self.args.dst_language, google_translate_api_key, dst=self.args.dst_language,
                                                        src=self.args.src_language)
                                prompt = "Translating from {0} to {1}: ".format(self.args.src_language, self.args.dst_language)
                                widgets = [prompt, Percentage(), ' ', Bar(), ' ', ETA()]
                                pbar = ProgressBar(widgets=widgets, maxval=len(regions)).start()
                                translated_transcripts = []
                                # self.progress.setValue(0)
                                for i, transcript in enumerate(pool.imap(translator, transcripts)):
                                    translated_transcripts.append(transcript)
                                    pbar.update(i)
                                    # self.progress.setValue(i)
                                pbar.finish()
                                # self.progress.setValue(100)
                                transcripts = translated_transcripts
                            else:
                                print "Error: Subtitle translation requires specified Google Translate API key. \
                                See --help for further information."
                                return 1

                    except KeyboardInterrupt:
                        pbar.finish()
                        pool.terminate()
                        pool.join()
                        print "Cancelling transcription"
                        return 1

                timed_subtitles = [(r, t) for r, t in zip(regions, transcripts) if t]
                formatter = FORMATTERS.get("srt")
                formatted_subtitles = formatter(timed_subtitles)

                base, ext = os.path.splitext(str(filename))
                dest = "{base}.{format}".format(base=base, format="srt")

                with open(dest, 'wb') as f:
                    f.write(formatted_subtitles.encode("utf-8"))

                print "Subtitles file created at {}".format(dest)
                # open_video(0, self.args.source_path)
                # print formatted_subtitles.split('\n')
                
            # create the media
            self.sec=-1
            if sys.version < '3':
                filename = unicode(filename)
            self.media = self.instance.media_new(filename)
            # put the media in the media player
            self.mediaplayer.set_media(self.media)

            # parse the metadata of the file
            self.media.parse()
            # set the title of the track as window title
            self.setWindowTitle(self.media.get_meta(0))
    
            # the media player has to be 'connected' to the QFrame
            # (otherwise a video would be displayed in it's own window)
           
            if sys.platform.startswith('linux'): # for Linux using the X Server
                self.mediaplayer.set_xwindow(self.videoframe.winId())
            elif sys.platform == "win32": # for Windows
                self.mediaplayer.set_hwnd(self.videoframe.winId())
            elif sys.platform == "darwin": # for MacOS
                self.mediaplayer.set_nsobject(self.videoframe.winId())
            self.PlayPause()
            self.duration = get_duration_wav(base + ".wav")
            f=open(self.sub_path)
            top_10 = {}
            pos = 0
            wrds = ['their','the','a','on','an','i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him', 'her', 'us', 'them', 'what', 'who', 'whom', 'mine', 'yours', 'his', 'hers', 'ours', 'theirs','this', 'that', 'these', 'those', 'with', 'at', 'by', 'into', 'for', 'to', 'up', 'of', 'in', 'is', 'are', 'and', 'as', 'if', 'from']
            for i in f.readlines():
                pos += 1
                if (pos + 1) % 4 == 0:
                    for j in i.lower().split():
                        if top_10.get(j) != None:
                            if j not in wrds:
                                top_10[j] += 1
                        else:
                            if j not in wrds:
                                top_10[j] = 1
            top = []
            self.comboBox.clear()
            for key, value in sorted(top_10.iteritems(), key=lambda (k,v): (v,k), reverse=True):
                print "%s: %s" % (key, value)
                top.append(value)
                self.comboBox.addItem(str(key))
                if len(top) == 10:
                    break
    def invalid_file(self):
        choice = QtGui.QMessageBox.question(self, "!", "File Not Found Or Invalid File Format", QtGui.QMessageBox.Ok)

    def err(self, msg):
        choice = QtGui.QMessageBox.question(self, "!", msg, QtGui.QMessageBox.Ok)
    
    def setVolume(self, Volume):
        """Set the volume
        """
        self.mediaplayer.audio_set_volume(Volume)

    def setPosition(self, position):
        """Set the position
        """
        # setting the position to where the slider was dragged
        self.mediaplayer.set_position(position / 1000.0)
        # the vlc MediaPlayer needs a float value between 0 and 1, Qt
        # uses integer variables, so you need a factor; the higher the
        # factor, the more precise are the results
        # (1000 should be enough)

    def updateUI(self):
        """updates the user interface"""
        # setting the slider to the desired position
        self.positionslider.setValue(self.mediaplayer.get_position() * 1000)

        if not self.mediaplayer.is_playing():
            # no need to call this function if nothing is played
            self.timer.stop()
            if not self.isPaused:
                # after the video finished, the play button stills shows
                # "Pause", not the desired behavior of a media player
                # this will fix it
                self.Stop()

def toSec(s):
    s = s.split(':')
    return int(s[0]) * 60 * 60 + int(s[1]) * 60 + int(s[2])

def search(word, sub_path):
    prev = ''
    t1 = []
    j = 0
    f=open(sub_path)
    for i in f.readlines():
        if i.lower().find(word.lower()) > 0:
            t1.append(prev)
            j += 1
        prev = i

    if j == 0:
        return t1, False
    return t1, True


if __name__ == "__main__":
    app = QtGui.QApplication(sys.argv)
    player = Player()
    player.show()
    player.resize(640, 480)
    if sys.argv[1:]:
        player.OpenFile(sys.argv[1])
    sys.exit(app.exec_())

