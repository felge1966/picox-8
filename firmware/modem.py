import machine
import cpld
import rp2
import time
import socket
import errno
from machine import Pin

from command_processor import CommandProcessor
from enum import Enum
import wifi
import telnet
import config

instance = None

TICK_MS = 10
TICKS_PER_SECOND = 1000 / TICK_MS

@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def tone_generator():
    pull()
    wrap_target()
    set(pins, 1)
    mov(x, osr)
    label("delay_high")
    jmp(x_dec, "delay_high")
    set(pins, 0)
    mov(x, osr)
    label("delay_low")
    jmp(x_dec, "delay_low")

tone_generator1 = rp2.StateMachine(4, tone_generator, freq=10_000_000, set_base=Pin(28))
tone_generator2 = rp2.StateMachine(5, tone_generator, freq=10_000_000, set_base=Pin(28))


def set_freq(sm, f):
    if f > 0:
        sm.active(0)
        delay = int(1/f/100e-9/2)-3
        sm.restart()
        sm.put(delay)
        sm.active(1)
    else:
        sm.restart()                                        # restart to set output to low if running


class Control(Enum):
  OHC = 0x01
  HSC = 0x02
  MON = 0x04
  TXC = 0x08
  ANS = 0x10
  TEST = 0x20
  PWR = 0x40
  CCT = 0x80

  @classmethod
  def get_names(cls, value):
    result = []
    for mask, name in cls._create_name_mapping().items():
      if value & mask:
        result.append(name)
    return result


class Status(Enum):
    RNG = 1
    CD = 4


class Event(Enum):
    CONTROL_OHC = 0
    CONTROL_MON = 1
    CONTROL_TXC = 2
    CONTROL_PWR = 3
    CONTROL_CCT = 4
    DTMF = 5
    TICK = 6
    UART_RX = 7


class State(Enum):
    IDLE = 0
    OFF_HOOK = 1
    DIALING = 2
    RINGING = 3
    ECHO_CANCEL = 4
    HANDSHAKE = 5
    CONNECTED = 6
    ENTER_COMMAND_MODE = 7
    COMMAND_MODE = 8
    CALL_FAILED = 9
    DRAIN_UART = 10
    TELNET_MODE = 11

class CallProgressTone:
    def __init__(self, tones, repeats=False):
        self.tones = tones
        self.repeats = repeats
        assert(len(self.tones) % 1 == 0)
        self.reset()

    def reset(self):
        self.pos = 0

    def done(self):
        return len(self.tones) == self.pos

    def next(self):
        frequency = self.tones[self.pos]
        duration = self.tones[self.pos+1]
        self.pos += 2
        if self.repeats and len(self.tones) == self.pos:
            self.pos = 0
        return frequency, duration


CONNECT_DELAY = 3000 # time to wait before opening the data channel after carrier detect was signalled

INVALID_NUMBER_TONE = CallProgressTone((950, 330, 1450, 330, 1880, 330, 0, 1000), repeats=True)
NO_NETWORK_TONE = CallProgressTone((425, 240, 0, 240), repeats=True)
BUSY_TONE = CallProgressTone((425, 480, 0, 430), repeats=True)
RING_TONE = CallProgressTone((425, 1000, 0, 4000))
ECHO_CANCEL_TONE = CallProgressTone((2100, 430, 20, 20) * 6 + (2225, 430, 20, 20) * 6)
HANDSHAKE_ANSWER_TONE = CallProgressTone((1650, CONNECT_DELAY))
HANDSHAKE_ORIGINATE_TONE = CallProgressTone((980, CONNECT_DELAY))
COMMAND_MODE_TONE = CallProgressTone((425, 240, 0, 240, 425, 240, 0, 3000))


class TonePlayer:
    def __init__(self, tone):
        self.tone = tone
        self.tone.reset()
        self.play_next()

    def play_next(self):
        frequency, duration = self.tone.next()
        set_freq(tone_generator1, frequency)
        self.ticks_remaining = duration / TICK_MS

    def tick(self):
        self.ticks_remaining -= 1
        if self.ticks_remaining > 0:
            return
        if self.tone.done():
            set_freq(tone_generator1, 0)
            return True
        self.play_next()


class Modem:
    def __init__(self, uart):
        global instance
        if instance:
            print('Warning: Modem instance already exists')
        instance = self
        self.uart = uart
        self.socket = None
        self.last_tick = time.ticks_ms()
        self.tick_count = 0
        self.reset()

    def reset(self):
        self.status = Status.RNG | Status.CD
        cpld.write_reg(cpld.REG_MODEM_STATUS, self.status)
        set_freq(tone_generator1, 0)
        set_freq(tone_generator2, 0)
        self.state = None
        self.set_state(State.IDLE)
        self.answer_mode = False
        self.old_modem_control = 0
        self.old_baud_control = 0
        self.dtmf_digit = None
        self.number_buffer = ''
        self.tone_player = None
        self.command_processor = None
        if self.socket:
            self.socket.close()
            self.socket = None

    def set_state(self, state):
        print("Modem", State.get_name(self.state), "->", State.get_name(state))
        self.state = state

    def call_failed(self, tone):
        self.tone_player = TonePlayer(tone)
        self.set_state(State.CALL_FAILED)

    def carrier_detected(self, on):
        if on:
            self.status = self.status & ~Status.CD
        else:
            self.status = self.status | Status.CD
        cpld.write_reg(cpld.REG_MODEM_STATUS, self.status)

    def ringing(self, on):
        if on:
            self.status = self.status & ~Status.RNG
        else:
            self.status = self.status | Status.RNG
        cpld.write_reg(cpld.REG_MODEM_STATUS, self.status)

    def handle_event(self, event, arg):
        if self.state == State.IDLE:
            if event == Event.CONTROL_OHC and arg:
                set_freq(tone_generator1, 425)
                self.set_state(State.OFF_HOOK)
                self.number_buffer = ''
            if event == Event.UART_RX:
                self.set_state(State.TELNET_MODE)
                self.handle_event(event, arg)               # send char to command processor
        elif self.state == State.OFF_HOOK:
            if event == Event.DTMF:
                self.number_buffer += arg
                self.tick_count = 0
                self.set_state(State.DIALING)
        elif self.state == State.DIALING:
            if event == Event.DTMF:
                self.tick_count = 0
                self.number_buffer += arg
                if self.number_buffer == '***':
                    self.carrier_detected(True)
                    self.tone_player = TonePlayer(COMMAND_MODE_TONE)
                    self.set_state(State.ENTER_COMMAND_MODE)
            if event == Event.TICK:
                self.tick_count += 1
                if self.tick_count == TICKS_PER_SECOND:
                    if not wifi.connected:
                        self.call_failed(NO_NETWORK_TONE)
                        return
                    phonebook = config.get('phonebook', {})
                    if not self.number_buffer in phonebook:
                        self.call_failed(INVALID_NUMBER_TONE)
                        return
                    phonebook_entry = phonebook[self.number_buffer]
                    host, port = phonebook_entry
                    call_address = wifi.resolve(host, int(port))
                    if not call_address:
                        self.call_failed(NO_NETWORK_TONE)
                        return
                    self.socket = socket.socket(socket.IPPROTO_TCP)
                    try:
                        self.socket.connect(call_address)
                    except OSError as e:
                        print(f'call failed {e}')
                        self.call_failed(BUSY_TONE)
                        return
                    self.socket.setblocking(False)
                    self.tone_player = TonePlayer(RING_TONE)
                    self.set_state(State.RINGING)
        elif self.state == State.CALL_FAILED:
            if event == Event.TICK:
                self.tone_player.tick()
        elif self.state == State.RINGING:
            if event == Event.TICK:
                if not self.tone_player.tick():
                    return
                self.tone_player = TonePlayer(ECHO_CANCEL_TONE)
                self.set_state(State.ECHO_CANCEL)
        elif self.state == State.ECHO_CANCEL:
            if event == Event.TICK:
                if not self.tone_player.tick():
                    return
                self.carrier_detected(True)
                self.tone_player = TonePlayer(HANDSHAKE_ANSWER_TONE if self.answer_mode else HANDSHAKE_ORIGINATE_TONE)
                self.set_state(State.HANDSHAKE)
        elif self.state == State.HANDSHAKE:
            if event == Event.TICK:
                if not self.tone_player.tick():
                    return
                try:
                    telnet.send_options(self.socket)
                except:
                    pass                                    # ignore errors during telnet option negotiation
                self.set_state(State.CONNECTED)
        elif self.state == State.CONNECTED:
            if event == Event.UART_RX:
                try:
                    self.socket.write(arg)
                except OSError as e:
                    print(f'Error {e} writing to socket, closing connection')
                    self.socket.close()
                    self.reset()
            if event == Event.TICK:
                try:
                    data = self.socket.recv(128)
                    if data:
                        data = telnet.process_options(self.socket, data)
                        self.uart.write(data)
                    else:
                        self.set_state(State.DRAIN_UART)
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        print(f'Error {e} reading from socket, closing connection')
                        self.set_state(State.DRAIN_UART)
        elif self.state == State.ENTER_COMMAND_MODE:
            if event == Event.TICK:
                if self.tone_player and self.tone_player.tick():
                    self.tone_player = None
                    self.command_processor = CommandProcessor(self.uart)
                    self.set_state(State.COMMAND_MODE)
        elif self.state == State.COMMAND_MODE:
            if event == Event.UART_RX:
                if self.command_processor.userinput(arg):
                    self.set_state(State.DRAIN_UART)
        elif self.state == State.DRAIN_UART:
            if event == Event.TICK:
                if self.uart.txdone():
                    print('UART tx done, resetting modem')
                    self.reset()


    def handle_control(self):
        byte = cpld.read_reg(cpld.REG_MODEM_CONTROL)
        if byte == 0:
            print("Reset modem")
            self.reset()
            return
        print("Modem Control:", Control.get_names(byte))
        self.answer_mode = byte & Control.ANS
        if (byte ^ self.old_modem_control) & Control.OHC:
            self.handle_event(Event.CONTROL_OHC, byte & Control.OHC)
        if (byte ^ self.old_modem_control) & Control.MON:
            self.handle_event(Event.CONTROL_MON, byte & Control.MON)
        if (byte ^ self.old_modem_control) & Control.TXC:
            self.handle_event(Event.CONTROL_TXC, byte & Control.TXC)
        if (byte ^ self.old_modem_control) & Control.PWR:
            self.handle_event(Event.CONTROL_PWR, byte & Control.PWR)
        if (byte ^ self.old_modem_control) & Control.CCT:
            self.handle_event(Event.CONTROL_CCT, byte & Control.CCT)


    DTMF_LOW = [ 697, 770, 852, 941 ]
    DTMF_HIGH = [ 1209, 1336, 1477, 1633 ]
    DTMF_FREQ_MAP = {
        0: '1',
        1: '2',
        2: '3',
        4: '4',
        5: '5',
        6: '6',
        8: '7',
        9: '8',
        10: '9',
        12: '*',
        13: '0'
    }

    def handle_tone_dialer(self):
        byte = cpld.read_reg(cpld.REG_TONE_DIALER)
        if byte & 0x10:
            high = byte & 0x03
            low = (byte & 0x0c) >> 2
            set_freq(tone_generator1, Modem.DTMF_LOW[low])
            set_freq(tone_generator2, Modem.DTMF_HIGH[high])
            self.dtmf_digit = Modem.DTMF_FREQ_MAP[byte & 0x0f]
        else:
            set_freq(tone_generator1, 0)
            set_freq(tone_generator2, 0)
            print(f'DTMF digit {self.dtmf_digit}')
            self.handle_event(Event.DTMF, self.dtmf_digit)

    def poll(self):
        if self.uart.any() > 0:
            self.handle_event(Event.UART_RX, self.uart.read())
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_tick) >= TICK_MS:
            self.last_tick = now
            self.handle_event(Event.TICK, None)
