from audioled import modulation, project, serverconfiguration
from audioled_controller import sysex_data
import mido
import logging
import pkg_resources
import json
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

class MidiProjectController:

    def __init__(self, callback=None):
        self._sendMidiCallback = callback

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
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(self._createVersionMsg())
        elif data[0] == 0x00 and data[1] == 0x01:
            # Active project metadata
            if self._sendMidiCallback is not None:
                metadata = serverconfig.getProjectMetadata(proj.id)
                self._sendMidiCallback(self._createActiveProjectMsg(metadata))
    
    def _createActiveProjectMsg(self, metadata):
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x01] + sysex_data.encode(json.dumps(metadata))
        return sendMsg

    def _createVersionMsg(self):
        version = "UNDEFINED"
        try:
            version = pkg_resources.get_distribution('molecole').version
        except Exception:
            # TODO: Sometimes breaks in unittests?
            pass
        sendMsg = mido.Message('sysex')
        sendMsg.data = [0x00, 0x00] + sysex_data.encode(version)
        return sendMsg

    def _handleProgramChange(self, program, proj):
        proj.activateScene(program)

        # Send current midi controller status
        status = proj.getControllerModulations()
        for controller, v in status.items():
            logger.info("Sending modulation controller value {} for controller {}".format(v, controller))
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

        # TODO: Send sysex for available controller
        status = proj.getController()
        controllerEnabled = {}
        for controller in modulation.allController:
            if controller in inverseControllerMap:
                controllerEnabled[inverseControllerMap[controller]] = False

        logger.info("Status: {}".format(status.keys()))
        for controller in status.keys():
            if controller in inverseControllerMap:
                controllerEnabled[inverseControllerMap[controller]] = True
        # Create midi message from map
        # TODO: Problem with receiving sysex in JUCE?
        # [StartOfExclusive, ID of vendor (non-commercial in this case), ... data ]
        if True:
            sysexData = [0xF0, 0x7D]
            for controllerNumber, enabled in controllerEnabled.items():
                sysexData += [controllerNumber, (0 if not enabled else 1)]
            sysexData += [0xF7]  # End of exclusive
            logger.info("Sending sysex {}".format(sysexData))
            sysexMsg = mido.Message.from_bytes(sysexData)
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(sysexMsg)
        # Version using note on / note off commands
        for controllerNumber, enabled in controllerEnabled.items():
            msg = None
            if enabled:
                msg = mido.Message('note_on')
            else:
                msg = mido.Message('note_off')
            msg.channel = 1
            msg.note = controllerNumber
            if self._sendMidiCallback is not None:
                self._sendMidiCallback(msg)

        # TODO: Send brightness
        # brightness = proj.getBrightness() # TODO: Implement
        # sendMsg = mido.Message('control_change')
        # sendMsg.channel = 1
        # sendMsg.control = 7
        # sendMsg.value = brightness * 127
        # midiBluetooth.sendMidi(sendMsg)
    
    def _handleControlChange(self, ctrl, value, proj):
        if ctrl in controllerMap:
            controlMsg = controllerMap[ctrl]
            controlVal = _ctrlToValue(controlMsg, value / 127)
            logger.debug("Propagating control change message")
            if controlMsg == modulation.CTRL_BRIGHTNESS:
                # Handle brightness globally
                proj.setBrightness(value / 127)
            else:
                proj.updateModulationSourceValue(0xFFF, controlMsg, controlVal)
        else:
            logger.warn("Unknown controller {}".format(ctrl))
