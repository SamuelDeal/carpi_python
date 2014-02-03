#!/usr/bin/env python2

import functools 
import pyudev 
import signal
import sys
import RPi.GPIO as GPIO
import threading
import time
import subprocess
import os
import shutil
import datetime
import logging
import traceback

# --- constants ---

GpioNextBtn = 15
GpioPauseBtn = 17
GpioPrevBtn = 18
GpioLed = 11
GpioPowerOut = 25

DiskBkp = 'rpi_trip'
FotoDir = 'photos'
UmountScript = '/usr/local/sbin/udev-auto-umount.sh'
IgnoredPartitions = ['boot']

# --- log mgnt ---

logger = None

def logError(errType, error, trace):
  global logger
  logger.error(error.message+"\n"+"\n\t".join(traceback.format_tb(trace)))

def daemonize():
  fileScript = os.path.basename(__file__).replace('.py', '', 1)
  sys.excepthook = logError
  if os.path.exists('/var/run/'+fileScript+'.pid'):
    sys.stderr.write("pid file already exists, abording\n")
    sys.exit(1)
  os.chdir("/")
  sys.stdout.flush()
  sys.stderr.flush()
  si = file('/dev/null', 'r')
  so = file('/dev/null', 'a+')
  se = file('/dev/null', 'a+')
  os.dup2(si.fileno(), sys.stdin.fileno())
  os.dup2(so.fileno(), sys.stdout.fileno())
  os.dup2(se.fileno(), sys.stderr.fileno())
  pid = os.fork()
  if pid > 0:
    sys.exit(0)
  pidfile = file('/var/run/'+fileScript+'.pid', 'w')
  pidfile.write(str(os.getpid()))
  pidfile.write("\n")
  pidfile.close()

def delPidFile():
  fileScript = os.path.basename(__file__).replace('.py', '', 1)
  if os.path.exists('/var/run/'+fileScript+'.pid'):
    os.remove('/var/run/'+fileScript+'.pid')
  

# --- app starting ---

if "--daemon" in sys.argv:
  fileScript = os.path.basename(__file__).replace('.py', '', 1)
  logging.basicConfig(filename='/var/log/'+fileScript+'.log', filemode='a', 
    format='%(asctime)s: %(levelname)s: %(message)s',
    level=logging.INFO)
  logger = logging.getLogger()
  daemonize()
else:
  logging.basicConfig(format='%(message)s', level=logging.INFO)
  logger = logging.getLogger()


logger.info('Starting...')

lock = threading.Lock()
bigDisk = False
sdCard = False
copying = False
sdCardName = None

class LedThread(threading.Thread):
    def __init__(self, pin):
        threading.Thread.__init__(self)
        self._time = -1
        self._on = False
        self._pin = pin
        self._stop = False
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pin, GPIO.OUT)
        self._updateEvent = threading.Event( ) 

    def _isFinished(self):
        global lock
        lock.acquire()
        finish = self._stop
        lock.release()
        return finish

    def run(self):
        while not self._isFinished():
            self._on = not self._on
            GPIO.output(self._pin, self._time == -1 or self._on)
            if(self._time == -1):
                self._updateEvent.wait()
            else:
                self._updateEvent.wait(self._time)
            self._updateEvent.clear()

    def stop(self):
        global lock
        lock.acquire()
        self._stop = True
        lock.release()
        self._updateEvent.set()

    def update(self, time):
        global lock
        lock.acquire()
        self._time = time
        lock.release()
        self._updateEvent.set()


class CopyThread(threading.Thread):
    def __init__(self, devName):
        threading.Thread.__init__(self)
        self._dev = devName
        self._stopEvent = threading.Event( ) 

    def run(self):
        global lock
        global copying
        global sdCard
        global bigDisk
        global logger
        lock.acquire()
        copying = True        
        lock.release()
        onChange()
        context = pyudev.Context() 
        for device in context.list_devices(subsystem='block', DEVTYPE='partition'):
            if device.get('ID_FS_LABEL_ENC') == DiskBkp: 
                df = subprocess.Popen(["/bin/df", device.get('DEVNAME')], stdout=subprocess.PIPE) 
                output = df.communicate()[0] 
                destSpace = int(output.split("\n")[1].split()[3])
            if device.get('ID_FS_LABEL_ENC') == self._dev:
                srcName = device.get('DEVNAME')
                df = subprocess.Popen(["/bin/df", srcName], stdout=subprocess.PIPE) 
                output = df.communicate()[0] 
                srcSize = int(output.split("\n")[1].split()[2])

        if srcSize > destSpace:
            logger.warning("Not enought space! Unable to save disk")
            lock.acquire()
            copying = False
            lock.release()
            onChange()
            return;   
        tstamp = os.path.getmtime('/media/'+self._dev)
        fileList = os.listdir('/media/'+self._dev)
        for file in fileList:
            if file[0] != '.':
                newtstamp = os.path.getmtime('/media/'+self._dev+'/'+file)
                if newtstamp > tstamp:
                    tstamp = newtstamp
        fileDate = datetime.date.fromtimestamp(tstamp)
        dirName = fileDate.strftime('%d_%m_%Y')
        if os.path.exists('/media/'+DiskBkp+'/'+FotoDir+'/'+dirName):
            i = 2
            newDir = dirName + ' ('+str(i)+')'
            while os.path.exists('/media/'+DiskBkp+'/'+FotoDir+'/'+newDir):
                ++i
                newDir = dirName + ' ('+str(i)+')'
            dirName = newDir
        logger.info('Copying '+ self._dev + ' to ' + dirName)
        shutil.copytree('/media/'+self._dev, '/media/'+DiskBkp+'/'+FotoDir+'/'+dirName)
        lock.acquire()
        copying = False
        sdCard = False
        lock.release()
        logger.info("Unmounting "+ srcName)
        subprocess.call([UmountScript, srcName], shell=False)
        onChange()

    def stop(self):
        self._stopEvent.set()

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

def onAdd(devName):
    global sdCard
    global bigDisk
    global led
    global sdCardName
    if devName == DiskBkp:
        lock.acquire()
        bigDisk = True
        lock.release()
        onChange()
    elif (devName not in IgnoredPartitions) and (devName != None):
        time.sleep(0.5)
        if(os.path.exists('/media/'+devName)): 
            lock.acquire()
            sdCard = True
            sdCardName = devName
            lock.release()
            onChange()

def onRemove(devName):
    global sdCard
    global bigDisk
    global led
    if devName == DiskBkp:
        lock.acquire()
        bigDisk = False
        lock.release()
        onChange()
    elif (devName not in IgnoredPartitions) and (devName != None): 
        lock.acquire()
        sdCard = False
        lock.release()
        onChange()

def onChange():
    global sdCard
    global bigDisk
    global led
    global copying
    global lock
    lock.acquire()
    if(bigDisk and sdCard):
        if copying:
            lock.release()
            led.update(0.1)
        else:
            lock.release()
            led.update(0.5)
    else:
        lock.release()
        led.update(-1) 

led = LedThread(GpioLed)
led.start()		

context = pyudev.Context() 
monitor = pyudev.Monitor.from_netlink(context) 
monitor.filter_by('block')
for device in context.list_devices(subsystem='block', DEVTYPE='partition'):
    onAdd(device.get('ID_FS_LABEL_ENC'))
if not bigDisk:
  logger.warning('Main hard drive is not connected')


GPIO.setup(GpioNextBtn, GPIO.IN, pull_up_down=GPIO.PUD_UP) 
GPIO.setup(GpioPauseBtn, GPIO.IN, pull_up_down=GPIO.PUD_UP) 
GPIO.setup(GpioPrevBtn, GPIO.IN, pull_up_down=GPIO.PUD_UP) 
GPIO.setup(GpioPowerOut, GPIO.IN, pull_up_down=GPIO.PUD_UP) 

def evt_udev(action, device):
  if 'ID_FS_TYPE' in device:
      if action == 'add':
          onAdd(device.get('ID_FS_LABEL_ENC'))
      else:
          onRemove(device.get('ID_FS_LABEL_ENC'))

observer = pyudev.MonitorObserver(monitor, evt_udev)
observer.start()

def onBtn1(arg):
    global bigDisk
    global sdCard
    global copying
    global sdCardName
    global lock
    lock.acquire()
    if bigDisk == False:
        lock.release()
        return;
    if sdCard and (copying == False):
        lock.release()
        cp = CopyThread(sdCardName)
        cp.start()
    else:
        lock.release()
        subprocess.call(['/usr/bin/mpc', 'toggle', '--quiet'], shell=False)

def onBtn2(arg):
    global bigDisk
    global sdCard
    global copying
    global sdCardName
    global lock
    lock.acquire()
    if bigDisk == False:
        lock.release()
        return;
    if sdCard and (copying == False):
        lock.release()
        cp = CopyThread(sdCardName)
        cp.start()
    else:
        lock.release()
        subprocess.call(['/usr/bin/mpc', 'next', '--quiet'], shell=False)

def onBtn3(arg):
    global bigDisk
    global sdCard
    global copying
    global sdCardName
    global lock
    lock.acquire()
    if bigDisk == False:
        lock.release()
        return;
    if sdCard and (copying == False):
        lock.release()
        cp = CopyThread(sdCardName)
        cp.start()
    else:
        lock.release()
        subprocess.call(['/usr/bin/mpc', 'prev', '--quiet'], shell=False)

def cleanAll():
  global observer
  global led
  global logger
  global GpioPauseBtn
  global GpioNextBtn
  global GpioPrevBtn
  global GpioPowerOut
  led.stop()
  GPIO.remove_event_detect(GpioPauseBtn)
  GPIO.remove_event_detect(GpioNextBtn)
  GPIO.remove_event_detect(GpioPrevBtn)
  GPIO.remove_event_detect(GpioPowerOut)
  GPIO.output(GpioLed, False)
  observer.stop()
  if "--daemon" in sys.argv:
    delPidFile()

def onPowerOff2(arg):
  global observer
  global led
  global logger
  global GpioPowerOut
  logger.info('Power off2')
  GPIO.remove_event_detect(GpioPowerOut)


def onPowerOff(arg):
  global observer
  global led
  global logger
  global GpioPowerOut
  logger.info('Power off')
  cleanAll()
  cmd = subprocess.Popen(["/sbin/poweroff"], stdout=subprocess.PIPE) 
  output = cmd.communicate()[0] 
  sys.exit(0)

def exitBySignal(signum = None, frame = None): 
  logger.info('Signal received, exiting')
  # cleanAll()
  # sys.exit(0)

for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT]: 
  signal.signal(sig, exitBySignal) 

GPIO.add_event_detect(GpioPauseBtn, GPIO.RISING, callback = onBtn1, bouncetime = 300) 
GPIO.add_event_detect(GpioNextBtn, GPIO.RISING, callback = onBtn2, bouncetime = 300)
GPIO.add_event_detect(GpioPrevBtn, GPIO.RISING, callback = onBtn3, bouncetime = 300)
GPIO.add_event_detect(GpioPowerOut, GPIO.RISING, callback = onPowerOff, bouncetime = 300)

if "--daemon" not in sys.argv:
  logger.info('Press Ctrl-C to quit')
signal.pause()
