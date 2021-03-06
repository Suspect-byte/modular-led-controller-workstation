from audioled import modulation, project, serverconfiguration, version, client_config, dsp, audio
from audioled_controller import sysex_data

from pyupdater.client import Client
from pyupdater.client.downloader import FileDownloader
import mido
import logging
import os
import json
import jsonpickle
import zlib
import glob
import sys
import threading
import signal
import shutil
from collections.abc import Iterable   # drop `.abc` with Python 2.7 or lower

logger = logging.getLogger(__name__)

controllerMap = {
    1: modulation.CTRL_MODULATION,  # mod wheel
    7: modulation.CTRL_BRIGHTNESS,  # volume
    11: modulation.CTRL_INTENSITY,  # expression
    21: modulation.CTRL_SPEED,  # unknown param?
    30: modulation.CTRL_PRIMARY_COLOR_R,
    31: modulation.CTRL_PRIMARY_COLOR_G,
    32: modulation.CTRL_PRIMARY_COLOR_B,
    40: modulation.CTRL_SECONDARY_COLOR_R,
    41: modulation.CTRL_SECONDARY_COLOR_G,
    42: modulation.CTRL_SECONDARY_COLOR_B
}
inverseControllerMap = {}
for k, v in controllerMap.items():
    inverseControllerMap[v] = k

# Helper to convert to json
def _ctrlToValue(ctrl, val):
    if ctrl == modulation.CTRL_PRIMARY_COLOR_R or ctrl == modulation.CTRL_SECONDARY_COLOR_R:
        return {"controllerAmount": 1., "r": val * 255}
    elif ctrl == modulation.CTRL_PRIMARY_COLOR_G or ctrl == modulation.CTRL_SECONDARY_COLOR_G:
        return {"controllerAmount": 1., "g": val * 255}
    elif ctrl == modulation.CTRL_PRIMARY_COLOR_B or ctrl == modulation.CTRL_SECONDARY_COLOR_B:
        return {"controllerAmount": 1., "b": val * 255}
    else:
        return {"controllerAmount": val}

class PathAndUrlDownloader(FileDownloader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._callback = kwargs.get("callback")
        logger.debug("Downloader for {}".format(args[0]))
        self._data = None
    
    def download_verify_return(self):
        # Download the data from the endpoint and return
        logger.debug("Download request for {} received".format(self.filename))
        if self._callback is not None:
            self._data = self._callback(self.filename)
        if self._data is None:
            self._data = super().download_verify_return()
        return self._data

    def download_verify_write(self):
        if self._data is None:
            self._data = self.download_verify_return()
        if self._data is not None:
            # Write the downloaded data to the current dir
            logger.info("Write request for {} received".format(self.filename))
            logger.info("Writing {} bytes to file {}".format(len(self._data), self.filename))
            with open(self.filename, 'wb') as f:
                f.write(self._data)

def print_status_info(info):
    total = info.get(u'total')
    downloaded = info.get(u'downloaded')
    status = info.get(u'status')
    logger.info("{}: {} of {} downloaded".format(status, downloaded, total))

class MidiProjectControllerOptions:
    def __init__(self):
        self.update_paths = None
        self.client_config = None
        self.update_urls = None

class MidiProjectController:

    def __init__(self, callback=None, options=None):
        self._sendMidiCallback = callback

        self._update_paths = None
        self._client_config = None
        self._isUpdating = False
        if options is not None:
            self._update_paths = options.update_paths
            self._client_config = options.client_config
            self._update_urls = options.update_urls
        # Client for pyupdate
        if self._client_config is None:
            self._client_config = client_config.ClientConfig()
        self.client = Client(self._client_config, downloader=self.createDownloader)
        self.client.add_progress_hook(print_status_info)

    def createDownloader(self, filename, urls, **kwargs):
        logger.debug("Create downloader for {}".format(filename))
        # replace urls from serverconfig
        urls = self._update_urls
        d = PathAndUrlDownloader(filename, urls, callback=self.downloadCallback)
        return d

    def downloadCallback(self, file):
        if self._update_paths is not None:
            for updatePath in self._update_paths:
                for p in glob.iglob(updatePath):
                    if os.path.isdir(p):
                        logger.info("Checking directory {}/{}".format(p, file))
                        path = os.path.join(p, file)
                        if os.path.isfile(path):
                            logger.info("Downloading from path {}".format(path))
                            return open(path, "rb").read()
                logger.debug("{} not found in {}".format(file, updatePath))
        return None

    def handleMidiMsg(self, msg: mido.Message, serverconfig: serverconfiguration.ServerConfiguration, proj: project.Project):
        # of type mido.Message
        # channel	0..15	0
        # frame_type	0..7	0
        # frame_value	0..15	0
        # control	0..127	0
        # note	0..127	0
        # program	0..127	0
        # song	0..127	0
        # value	0..127	0
        # velocity	0..127	64
        # data	(0..127, 0..127, …)	() (empty tuple)
        # pitch	-8192..8191	0
        # pos	0..16383	0
        # time	any integer or float	0

        if msg.type == 'program_change':
            self._handleProgramChange(msg.program, proj)
        elif msg.type == 'control_change':
            self._handleControlChange(msg.control, msg.value, proj)
        elif msg.type == 'sysex':
            self._handleSysex(msg.data, serverconfig, proj)

    def _handleSysex(self, data, serverconfig: serverconfiguration.ServerConfiguration, proj: project.Project):
        if len(data) < 2:
            logger.error("Sysex message too short")
            return

        if data[0] == 0x00 and data[1] == 0x00:
            # Version
            logger.info("MIDI-BLE REQ Version")
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createVersionMsg())
        elif data[0] == 0x00 and data[1] == 0x10:
            # Update
            logger.info("MIDI-BLE REQ Update")
            logger.info("Checking for update, current version is {}".format(version.get_version()))
            self._updatePathsAndUrls(serverconfig)
            if not self._isUpdating:
                self._isUpdating = True
                # New Downloaders will be created on request
                self.client.refresh()
                app_update = self.client.update_check('Molecole', version.get_version())
                if app_update is not None:
                    logger.info("Update {} available".format(app_update.current_version))
                    threading.Thread(target=self._updateApp, args=[app_update]).start()
                else:
                    self._isUpdating = False
                    logger.info("Update check returned no update")
                    if self._sendMidiCallback is not None:
                        self._sendMidiCallback(self._createUpdateNotAvailableMsg())
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createUpdateBusyMsg())
        elif data[0] == 0x00 and data[1] == 0x11:
            # Update check
            logger.info("MIDI-BLE REQ Update check")
            logger.info("Checking for update, current version is {}".format(version.get_version()))
            self._updatePathsAndUrls(serverconfig)

            if not self._isUpdating:
                self._isUpdating = True
                try:
                    # Move version.gz to force a reload
                    oldFile = os.path.join(self.client.data_dir, self.client.version_file)
                    if os.path.exists(oldFile):
                        shutil.move(oldFile, oldFile.replace('.gz', '.gz.bak'))

                    oldFile = os.path.join(self.client.data_dir, self.client.version_file_compat)
                    if os.path.exists(oldFile):
                        shutil.move(oldFile, oldFile.replace('.gz', '.gz.bak'))
                        
                    self.client.refresh()
                    app_update = self.client.update_check('Molecole', version.get_version())
                    self._isUpdating = False
                    if app_update is not None:
                        logger.info("Update {} available".format(app_update.version))
                        if self._sendMidiCallback is not None:
                            self._sendMidiCallback(self._createUpdateVersionAvailableMsg(app_update.version))
                    else:
                        logger.info("Update check returned no update")
                        if self._sendMidiCallback is not None:
                            self._sendMidiCallback(self._createUpdateNotAvailableMsg())
                except Exception as e:
                    logger.error("Error trying to update: {}".format(e))
                    self._isUpdating = False
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createUpdateBusyMsg())
        elif data[0] == 0x00 and data[1] == 0x20:
            # Active project metadata
            if self._sendMidiCallback is not None:
                metadata = serverconfig.getProjectMetadata(proj.id)
                self._sendMidiCallback(self._createActiveProjectMsg(metadata))
        elif data[0] == 0x00 and data[1] == 0x30:
            # Get projects
            if self._sendMidiCallback is not None:
                metadata = serverconfig.getProjectsMetadata()
                self._sendMidiCallback(self._createProjectsMsg(metadata))
        elif data[0] == 0x00 and data[1] == 0x40:
            # Activate project
            projUid = str(bytes(sysex_data.decode(data[2:])), encoding='utf8')
            logger.info("MIDI-BLE REQ Activate project {}".format(projUid))
            proj = serverconfig.getProject(projUid)
            if proj is not None:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createActivateProjSuccessfulMsg())
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createActivateProjNotFoundMsg())
        elif data[0] == 0x00 and data[1] == 0x50:
            # Import project
            logger.info("MIDI-BLE REQ Import project")
            dec = sysex_data.decode(data[2:])
            projGzip = zlib.decompress(bytes(dec))
            projJson = str(projGzip, encoding='utf8')
            try:
                serverconfig.importProject(projJson)
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createImportProjSuccessfulMsg())
            except Exception:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createImportProjErrorMsg())
        elif data[0] == 0x00 and data[1] == 0x60:
            # Export project
            byteArr = bytes(sysex_data.decode(data[2:]))
            logger.debug("Decoded {} to {}".format([hex(c) for c in data[2:]], byteArr))
            projUid = str(byteArr, encoding='utf8')
            logger.info("MIDI-BLE REQ Export project {}".format(projUid))
            proj = serverconfig.getProject(projUid)
            if proj is not None:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createExportProjSuccessfulMsg(proj))
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createExportProjNotFoundMsg())
        elif data[0] == 0x00 and data[1] == 0x70:
            # Activate project
            projUid = str(bytes(sysex_data.decode(data[2:])), encoding='utf8')
            logger.info("MIDI-BLE REQ Delete project {}".format(projUid))
            proj = serverconfig.getProject(projUid)
            if proj is not None:
                serverconfig.deleteProject(projUid)
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._deleteProjSuccessfulMsg())
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._deleteProjNotFoundMsg())
        elif data[0] == 0x01 and data[1] == 0x00:
            # Get active scene ID
            logger.info("MIDI-BLE REQ Active scene index")
            sceneId = serverconfig.getActiveProjectOrDefault().activeSceneId
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createGetActiveSceneIdMsg(sceneId))
        elif data[0] == 0x01 and data[1] == 0x10:
            # Get active scene metadata
            logger.info("MIDI-BLE REQ Get active scene metadata")
            proj = serverconfig.getActiveProjectOrDefault()
            metadata = proj.getSceneMetadata(proj.activeSceneId)
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createActiveSceneMetadataMsg(metadata))
        elif data[0] == 0x01 and data[1] == 0x20:
            # Get scenes metadata
            logger.info("MIDI-BLE REQ Get scenes")
            proj = serverconfig.getActiveProjectOrDefault()
            metadata = proj.sceneMetadata
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createScenesMetadataMsg(metadata))
        elif data[0] == 0x01 and data[1] == 0x30:
            # Get enabled controllers for active scene
            logger.info("MIDI-BLE REQ Get controller enabled for active scene")
            proj = serverconfig.getActiveProjectOrDefault()
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createEnabledControllersMsg(proj))
        elif data[0] == 0x01 and data[1] == 0x40:
            # Request controller values for active project
            logger.info("MIDI-BLE REQ Get controller values for active scene")
            proj = serverconfig.getActiveProjectOrDefault()
            self._sendControllerStatus(proj)
        elif data[0] == 0x01 and data[1] == 0x50:
            # Reset controller values for active project
            logger.info("MIDI-BLE REQ Reset controller for active scene")
            proj = serverconfig.getActiveProjectOrDefault()
            proj.resetControllerModulation()
            self._sendControllerStatus(proj)
        elif data[0] == 0x02 and data[1] == 0x00:
            # Get server configuration
            logger.info("MIDI-BLE REQ Get server configuration")
            config = serverconfig.getFullConfiguration()
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createGetServerConfigMsg(config))
        elif data[0] == 0x02 and data[1] == 0x10:
            # Update server configuration
            logger.info("MIDI-BLE REQ Update server configuration")
            dec = sysex_data.decode(data[2:])
            configGzip = zlib.decompress(bytes(dec))
            configJson = str(configGzip, encoding='utf8')
            config = json.loads(configJson)
            try:
                serverconfig.setConfiguration(config)
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createUpdateServerConfigSuccessfulMsg())
            except Exception:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createUpdateServerConfigErrorMsg())
        elif data[0] == 0x02 and data[1] == 0x20:
            logger.info("MIDI-BLE REQ Audio rms")
            rms = {"0": "NO_BUFFER"}
            if audio.GlobalAudio.buffer is not None:
                if isinstance(audio.GlobalAudio.buffer, Iterable):
                    numChannels = len(audio.GlobalAudio.buffer)

                    for i in range(numChannels):
                        rmsFromChannel = dsp.rms(audio.GlobalAudio.buffer[i])
                        rms[str(i)] = rmsFromChannel
                else:
                    logger.debug("audio.GlobalAudio.buffer not iterateable")
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createAudioRMSMsg(json.dumps(rms)))
        else:
            logger.error("MIDI-BLE Unknown sysex {} {}".format(hex(data[0]), hex(data[1])))

    def _createAudioRMSMsg(self, rms):
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x02, 0x20] + sysex_data.encode("{}".format(rms))
        return sendMsg

    def _createGetServerConfigMsg(self, config: dict):
        logger.debug("MIDI-BLE RESPONSE Get server config - Successful")
        json = jsonpickle.dumps(config)
        gzip = zlib.compress(bytes(json, encoding='utf8'))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x02, 0x00] + sysex_data.encode(gzip)
        return sendMsg

    def _createUpdateServerConfigSuccessfulMsg(self):
        logger.debug("MIDI-BLE RESPONSE Update server config - Successful")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x02, 0x10]
        return sendMsg

    def _createUpdateServerConfigErrorMsg(self):
        logger.debug("MIDI-BLE RESPONSE Update server config - Successful")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x02, 0x1F]
        return sendMsg

    def _updateApp(self, app_update):
        try:
            logger.debug("Starting download in background")
            threading.current_thread().name = 'UpdateThread'
            app_update.download()
            
            if app_update.is_downloaded():
                logger.info("Update downloaded")
                if not getattr(sys, 'frozen', False):
                    logger.info("Not running from executable. Extract only")
                    logger.debug("Extracting update")
                    if app_update.extract():
                        logger.debug("Extract succeeded")
                    else:
                        logger.debug("Extract not successful")
                    logger.debug("Extract done")
                else:
                    logger.info("Extracting and restarting...")
                    app_update.extract_overwrite()
                    logger.info("Extracting done. Killing server")
                    os.kill(os.getpid(), signal.SIGUSR1)
            else:
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(self._createUpdateNotAvailableMsg())
            logger.debug("End of update")
        finally:
            self._isUpdating = False

    def _createEnabledControllersMsg(self, proj):
        status = proj.getController()
        controllerEnabled = {}
        for controller in modulation.allController:
            if controller in inverseControllerMap:
                controllerEnabled[inverseControllerMap[controller]] = False

        logger.debug("Status: {}".format(status.keys()))
        for controller in status.keys():
            if controller in inverseControllerMap:
                controllerEnabled[inverseControllerMap[controller]] = True

        logger.debug("MIDI-BLE RESPONSE Enabled controllers for active scene: {}".format(controllerEnabled))
        
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x01, 0x30] + sysex_data.encode(json.dumps(controllerEnabled))
        return sendMsg

    def _createScenesMetadataMsg(self, metadata):
        logger.debug("MIDI-BLE RESPONSE Get scenes metadata {}".format(metadata))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x01, 0x20] + sysex_data.encode(json.dumps(metadata))
        return sendMsg

    def _createActiveSceneMetadataMsg(self, metadata):
        logger.debug("MIDI-BLE RESPONSE Get active scene metadata {}".format(metadata))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x01, 0x10] + sysex_data.encode(json.dumps(metadata))
        return sendMsg

    def _createGetActiveSceneIdMsg(self, sceneId: int):
        logger.debug("MIDI-BLE RESPONSE Get active scene id {}".format(sceneId))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x01, 0x00] + sysex_data.encode("{}".format(sceneId))
        return sendMsg

    def _createExportProjSuccessfulMsg(self, proj):
        logger.debug("MIDI-BLE RESPONSE Export project - Successful")
        projJson = jsonpickle.dumps(proj)
        projGzip = zlib.compress(bytes(projJson, encoding='utf8'))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x60] + sysex_data.encode(projGzip)
        return sendMsg
    
    def _createExportProjNotFoundMsg(self):
        logger.debug("MIDI-BLE RESPONSE Export project - Not found")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x6F]
        return sendMsg

    def _deleteProjSuccessfulMsg(self):
        logger.debug("MIDI-BLE RESPONSE Delete project - Successful")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x70]
        return sendMsg
    
    def _deleteProjNotFoundMsg(self):
        logger.debug("MIDI-BLE RESPONSE Delete project - Not found")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x7F]
        return sendMsg

    def _createImportProjSuccessfulMsg(self):
        logger.debug("MIDI-BLE RESPONSE Import project - Successful")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x50]
        return sendMsg

    def _createImportProjErrorMsg(self):
        logger.debug("MIDI-BLE RESPONSE Import project - Error")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x5F]
        return sendMsg

    def _createActivateProjSuccessfulMsg(self):
        logger.debug("MIDI-BLE RESPONSE Activate project - Successful")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x40]
        return sendMsg
    
    def _createActivateProjNotFoundMsg(self):
        logger.debug("MIDI-BLE RESPONSE Activate project - Project not found")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x4F]
        return sendMsg

    def _createUpdateVersionAvailableMsg(self, version):
        logger.debug("MIDI-BLE RESPONSE Update to version {} available".format(version))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x11] + sysex_data.encode(version)
        return sendMsg

    def _createUpdateNotAvailableMsg(self):
        logger.debug("MIDI-BLE RESPONSE No update available")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x1F]
        return sendMsg

    def _createUpdateBusyMsg(self):
        logger.debug("MIDI-BLE RESPONSE Update or update check running. I'm busy.")
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x1E]
        return sendMsg
    
    def _createActiveProjectMsg(self, metadata):
        data = json.dumps(metadata)
        logger.debug("MIDI-BLE RESPONSE Active project {}".format(data))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x20] + sysex_data.encode(data)
        return sendMsg

    def _createProjectsMsg(self, metadata):
        data = json.dumps(metadata)
        logger.debug("MIDI-BLE RESPONSE project metadata: {}".format(data))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x30] + sysex_data.encode(data)
        return sendMsg

    def _createVersionMsg(self):
        v = version.get_version()
        logger.debug("MIDI-BLE RESPONSE Current Version {}".format(v))
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x00] + sysex_data.encode(v)
        return sendMsg

    def _sendControllerStatus(self, proj):
        # Send current midi controller status
        status = proj.getControllerModulations()
        for controller, v in status.items():
            logger.debug("Sending modulation controller value {} for controller {}".format(v, controller))
            sendMsg = mido.Message('control_change')
            if controller in inverseControllerMap:
                sendMsg.channel = 1
                sendMsg.control = inverseControllerMap[controller]
                if sendMsg.control >= 30:
                    # scale color data
                    sendMsg.value = int(v / 255 * 127)
                else:
                    sendMsg.value = int(v * 127)
                if self._sendMidiCallback is not None:
                    self._sendMidiCallback(sendMsg)
        # Send current brightness value
        brightness = proj.getBrightnessActiveScene()
        if brightness is not None:
            sendMsg = mido.Message('control_change')
            sendMsg.channel = 1
            sendMsg.control = 7
            sendMsg.value = int(brightness * 127)
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(sendMsg)

    def _getUpdatePaths(self, paths: str):
        if ',' in paths:
            paths = paths.split(',')
        else:
            paths = [paths]
        return paths

    def _handleProgramChange(self, program, proj):
        proj.activateScene(program)

        self._sendControllerStatus(proj)

        # Send enabled controllers via sysex
        if self._sendMidiCallback is not None:
            self._sendMidiCallback(self._createEnabledControllersMsg(proj))

        

    
    def _handleControlChange(self, ctrl, value, proj):
        if ctrl in controllerMap:
            controlMsg = controllerMap[ctrl]
            controlVal = _ctrlToValue(controlMsg, value / 127)
            logger.debug("Propagating control change message")
            if controlMsg == modulation.CTRL_BRIGHTNESS:
                # Handle brightness globally
                proj.setBrightnessForActiveScene(value / 127)
            else:
                proj.updateModulationSourceValue(0xFFF, controlMsg, controlVal)
        else:
            logger.warn("Unknown controller {}".format(ctrl))

    def _updatePathsAndUrls(self, serverconfig):
        updatePath = serverconfig.getConfiguration(serverconfiguration.CONFIG_UPDATER_AUTOCHECK_PATH)
        logger.info("Scanning configuration for local directories {}".format(updatePath))
        updateUrls = serverconfig.getConfiguration(serverconfiguration.CONFIG_UPDATER_URL)
        self._update_paths = self._getUpdatePaths(updatePath)
        if updateUrls is not None and isinstance(updateUrls, str):
            self._update_urls = updateUrls.split(",")
        logger.info("Scanning local directories {} and urls {}".format(self._update_paths, self._update_urls))