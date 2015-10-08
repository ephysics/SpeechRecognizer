#!/usr/bin/python
# -*- coding: utf-8 -*-

#
# This is speech recognizer script (Google Speech API).
# It is based on Travis Payton's original script (see links below)
# 
# Modifications:
# - added voice-triggered recording (sox-based)
# - start sending audio data to upstream as soon as you start speaking (HTTP chunked, like in Chrome)
# - remove FLAC file parsing (using fixed sample rate)
# - added language parameter
# - print only final transcription result to STDOUT, for scripting
# - enhanced logging
# 
# Links:
#  - http://codeabitwiser.com/2014/09/python-google-speech-api/
#  - http://codeabitwiser.com/wp-content/uploads/2014/09/gsst.zip
#  - http://blog.travispayton.com/wp-content/uploads/2014/03/Google-Speech-API.pdf
#

import os
import random
import json
import getopt
import glob
from threading import Thread
from struct import *
import time
import sys
import logging
from subprocess import Popen, PIPE

import requests  # External Library http://docs.python-requests.org/en/latest/user/install/#install

# should use http://pythonhosted.org/pysox/ in future

class GoogleSpeechAPI(object):
    result = ''
    length = 0
    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36"
    UPSTREAM_URL_FORMAT  ='https://www.google.com/speech-api/full-duplex/v1/up?key=%(key)s&pair=%(pair)s&lang=%(lang)s&client=chromium&continuous&interim&pFilter=0'
    DOWNSTREAM_URL_FORMAT="https://www.google.com/speech-api/full-duplex/v1/down?pair=%(pair)s"
    API_KEY="AIzaSyCkfPOPZXDKNn8hhgu3JrA62wIgC93d44k" # Chromiuim key
    #AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw # Chrome from debian
    #AIzaSyC53OK59kFcMRMXtxxNujBUHU130A6XvZo # from https://gist.github.com/alotaiba/1730160
    SAMPLE_RATE = 16000 # Google Chrome does so 
    REC_COMMAND_FORMAT="/usr/bin/rec -q -V0 %(filename)s rate %(rate)s silence 1 0.05 8%% 1 1.0 8%%" # see man sox

    def __init__(self, record_filename, language):
        '''
        record_filename - temporary file in current directory for storing flac audiodata
        language - recognition langage for Google (e.g. 'ru-RU', 'en-US')
        '''
        self.log = logging.getLogger(__name__)

        self.record_filename = record_filename
        self.record_command = self.REC_COMMAND_FORMAT % {"filename":self.record_filename, "rate": self.SAMPLE_RATE}

        pair = self.getPair()
        self.lang = language
        self.upstream_headers = {
            'Content-Type': 'audio/x-flac; rate=%d' % self.SAMPLE_RATE, 
            "User-Agent": self.USER_AGENT
            }

        self.downstream_headers = {"User-Agent": self.USER_AGENT}

        self.upstream_url   = self.UPSTREAM_URL_FORMAT   % {"pair": pair, "key": self.API_KEY, "lang": self.lang}
        self.downstream_url = self.DOWNSTREAM_URL_FORMAT % {"pair": pair, "key": self.API_KEY}

        self.timeSinceResponse = 0
        self.response = ""
        self.connectionSuccessful = False
        self.no_result = False

    def getPair(self):
        return hex(random.getrandbits(64))[2:-1]

    def start(self):
        # remove previous file
        os.remove(self.record_filename)

        # start rec process and wait for file becomes not empty
        self.log.debug("Starting external rec process (%s)" % self.record_command)
        self.rec_process = Popen(self.record_command.split())
        self.log.debug("Start waiting for flac data available...")
        filesize = 0
        while filesize == 0:
            time.sleep(0.1)
            filesize = os.stat(self.record_filename).st_size

        self.log.debug("Flac data became available (%d), opening streams immediately..." % filesize)

        # file has some data in it, start HTTP streams and send data
        self.upsession = requests.Session()
        self.downsession = requests.Session()
        self.upstream_thread = Thread(target=self.upstream, args=(self.upstream_url,))
        self.downstream_thread = Thread(target=self.downstream, args=(self.downstream_url,))

        self.downstream_thread.start()
        self.upstream_thread.start()

        self.stop()

    def stop(self):
        self.downstream_thread.join()
        self.upstream_thread.join()

    def gen_data(self):
        file = open(self.record_filename,'rb')
        file.seek(0)
        counter = 0

        while True:
            item = file.read()
            if item:
                self.log.debug("%d bytes sent" % len(item))
                counter += len(item)
                yield item

            self.rec_process.poll()
            if self.rec_process.returncode != None:
                self.log.debug("Rec process is terminated, closing upload stream")
                self.log.debug("Total sent: %d bytes" % counter)
                return

    def final(self):
        try:
          response = json.loads(self.response)
          if response['result']:
              if 'final' in response['result'][0]:
                  return response['result'][0]['final']
        except Exception, e:
          # assuming invalid JSON, return False
          self.log.warning("exception testing latest line for final: '%s'" % self.response)
        return False

    def decode_transcript(self, response):
        try:
          response = json.loads(response)
          text = response['result'][0]['alternative'][0]['transcript']
          return text
        except Exception, e:
          # assuming invalid JSON, return False
          self.log.warning("exception testing latest line for final: '%s'" % self.response)
          self.log.error("exception !!!!" )
        return False

    def upstream(self, url):
        self.log.debug("Opening upstream URL %s",self.upstream_url)
        result = self.upsession.post(url, headers=self.upstream_headers, stream=True, data=self.gen_data())
        upstream_request_status = result.status_code
        upstream_request_content = result.content
        if upstream_request_status != 200:
            self.log.warning("failed request, status code %d, info: %s" % (upstream_request_status,result.content))
            self.start()
            raise RuntimeException("upstream request exception")
        self.log.info("request upstream content submission response is: %s" % upstream_request_content)

    def downstream(self, url):
        self.log.debug("Opening downstream URL %s",self.downstream_url)
        r = self.downsession.get(url, headers=self.downstream_headers, stream=True)
        self.status_code = r.status_code
        self.log.info("Response has been read (code %s), start parsing" % r.status_code)

        if r.status_code == 200:
            for line in r.iter_lines(): #response_content.splitlines():
                self.log.info(line.decode('UTF-8'))
                self.timeSinceResponse = 0
                self.response = line
                if line == '{"result":[]}':
                    # Google sends back an empty result signifying a successful connection
                    if not self.connectionSuccessful:
                        self.connectionSuccessful = True
                    else: # another empty response means Google couldn't find anything in the audio ...
                        self.log.info("No Recongnizable Dialogue, closing stream")
                        # Making pretty for result repacker
                        self.result.append('{"result":[{"alternative":[{"transcript":"","confidence":0.99999}],"final":true}],"result_index":0}')
                        self.no_result = True
                if self.final():
                    self.result = self.decode_transcript(self.response) # take only last line
                    break;
        else:
            self.log.warning("Failed to connect downstream. Response is: %s \n %s" %(r.status_code, r.content))
            self.log.info("Restarting Attempt")
            self.start()

# Main Transcription Loop
def main(argv):

    # uncomment this to enable logging
    #logging.basicConfig(format="%(asctime)-15s %(pathname)s:%(lineno)d %(message)s", filename="output.log", level="DEBUG")

    flac = "/home/user/scripts/google-speech/recording.flac" # temporary flac output file
    lang = 'ru-RU' if len(argv)==0 else argv[0]
    result = GoogleSpeechAPI(flac, lang)
    result.start()

    print result.result.encode('UTF-8')

if __name__ == "__main__":
    main(sys.argv[1:])