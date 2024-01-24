"""
Exposes several methods for transmitting cyclic messages.

The main entry point to these classes should be through
:meth:`can.BusABC.send_periodic`.
"""

import abc
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable, Final, Optional, Sequence, Tuple, Union

from can import typechecking
from can.message import Message

if TYPE_CHECKING:
    from can.bus import BusABC


# try to import win32event for event-based cyclic send task (needs the pywin32 package)
USE_WINDOWS_EVENTS = False
try:
    import pywintypes
    import win32event

    # Python 3.11 provides a more precise sleep implementation on Windows, so this is not necessary.
    # Put version check here, so mypy does not complain about `win32event` not being defined.
    if sys.version_info < (3, 11):
        USE_WINDOWS_EVENTS = True
except ImportError:
    pass

log = logging.getLogger("can.bcm")

NANOSECONDS_IN_SECOND: Final[int] = 1_000_000_000


class CyclicTask(abc.ABC):
    """
    Abstract Base for all cyclic tasks.
    """

    @abc.abstractmethod
    def stop(self) -> None:
        """Cancel this periodic task.

        :raises ~can.exceptions.CanError:
            If stop is called on an already stopped task.
        """


class CyclicSendTaskABC(CyclicTask, abc.ABC):
    """
    Message send task with defined period
    """

    def __init__(
        self, messages: Union[Sequence[Message], Message], period: float
    ) -> None:
        """
        :param messages:
            The messages to be sent periodically.
        :param period: The rate in seconds at which to send the messages.

        :raises ValueError: If the given messages are invalid
        """
        messages = self._check_and_convert_messages(messages)
        self.msgs_len = len(messages)
        self.messages = messages
        # Take the Arbitration ID of each message and put them into a list
        self.arbitration_id = [self.messages[idx].arbitration_id for idx in range(self.msgs_len)]
        self.period = period
        self.period_ns = int(round(period * 1e9))
        self.msg_index = 0

    @staticmethod
    def _check_and_convert_messages(
        messages: Union[Sequence[Message], Message]
    ) -> Tuple[Message, ...]:
        """Helper function to convert a Message or Sequence of messages into a
        tuple, and raises an error when the given value is invalid.

        Performs error checking to ensure that all Messages have the same channel.

        Should be called when the cyclic task is initialized.

        :raises ValueError: If the given messages are invalid
        """
        if not isinstance(messages, (list, tuple)):
            if isinstance(messages, Message):
                messages = [messages]
            else:
                raise ValueError("Must be either a list, tuple, or a Message")
        if not messages:
            raise ValueError("Must be at least a list or tuple of length 1")
        messages = tuple(messages)

        all_same_channel = all(
            message.channel == messages[0].channel for message in messages
        )
        if not all_same_channel:
            raise ValueError("All Channel IDs should be the same")

        return messages


class LimitedDurationCyclicSendTaskABC(CyclicSendTaskABC, abc.ABC):
    def __init__(
        self,
        messages: Union[Sequence[Message], Message],
        period: float,
        duration: Optional[float],
    ) -> None:
        """Message send task with a defined duration and period.

        :param messages:
            The messages to be sent periodically.
        :param period: The rate in seconds at which to send the messages.
        :param duration:
            Approximate duration in seconds to continue sending messages. If
            no duration is provided, the task will continue indefinitely.

        :raises ValueError: If the given messages are invalid
        """
        super().__init__(messages, period)
        self.duration = duration


class RestartableCyclicTaskABC(CyclicSendTaskABC, abc.ABC):
    """Adds support for restarting a stopped cyclic task"""

    @abc.abstractmethod
    def start(self) -> None:
        """Restart a stopped periodic task."""


class ModifiableCyclicTaskABC(CyclicSendTaskABC, abc.ABC):
    def _check_modified_messages(self, messages: Tuple[Message, ...]) -> None:
        """Helper function to perform error checking when modifying the data in
        the cyclic task.

        Performs error checking to ensure the arbitration ID and the number of
        cyclic messages hasn't changed.

        Should be called when modify_data is called in the cyclic task.

        :raises ValueError: If the given messages are invalid
        """
        if self.msgs_len != len(messages):
            raise ValueError(
                "The number of new cyclic messages to be sent must be equal to "
                "the number of messages originally specified for this task"
            )
        for idx in range(self.msgs_len):
            if self.arbitration_id[idx] != messages[idx].arbitration_id:
                raise ValueError(
                    "The arbitration ID of new cyclic messages cannot be changed "
                    "from when the task was created"
                )

    def modify_data(self, messages: Union[Sequence[Message], Message]) -> None:
        """Update the contents of the periodically sent messages, without
        altering the timing.

        :param messages:
            The messages with the new :attr:`Message.data`.

            Note: The arbitration ID cannot be changed.

            Note: The number of new cyclic messages to be sent must be equal
            to the original number of messages originally specified for this
            task.

        :raises ValueError: If the given messages are invalid
        """
        messages = self._check_and_convert_messages(messages)
        self._check_modified_messages(messages)

        self.messages = messages

class VariableRateCyclicTaskABC(CyclicSendTaskABC, abc.ABC):
    """A Cyclic task that supports a group period and intra-message period."""
    def _check_and_apply_period_intra(
        self, period_intra: Optional[float]
    ) -> None:
        """
        Helper function that checks if the given period_intra is valid and applies the 
        variable rate attributes to be used in the cyclic task.
        
        :param period_intra:
            The period in seconds to send intra-message.
            
        :raises ValueError: If the given period_intra is invalid
        """
        self._is_variable_rate = False
        self._run_cnt_msgs = [0]
        self._run_cnt_max = 1
        self._run_cnt = 0
        
        if period_intra is not None:
            if not isinstance(period_intra, float):
                raise ValueError("period_intra must be a float")
            if period_intra <= 0:
                raise ValueError("period_intra must be greater than 0")
            if self.msgs_len <= 1:
                raise ValueError("period_intra can only be used with multiple messages")
            if period_intra*self.msgs_len >= self.period:
                raise ValueError("period_intra per intra-message must be less than period")
            period_ms = int(round(self.period * 1000, 0))
            period_intra_ms = int(round(period_intra * 1000, 0))
            (_run_period_ms, msg_cnts, group_cnts) = self._find_gcd(period_ms, period_intra_ms)
            self._is_variable_rate = True
            self._run_cnt_msgs = [i*msg_cnts for i in range(self.msgs_len)]
            self._run_cnt_max = group_cnts
            self._run_cnt = 0
            # Override period, period_ms, and period_ns to be the variable period
            self.period = _run_period_ms / 1000
            self.period_ms = _run_period_ms
            self.period_ns = _run_period_ms * 1000000
    
    @staticmethod
    def _find_gcd(
        period_ms: int,
        period_intra_ms: int,
    ) -> Tuple[int, int, int]:
        """
        Helper function that finds the greatest common divisor between period_ms and period_intra_ms.
        
        :returns: 
            Tuple of (gcd_ms, m_steps, n_steps)
            * gcd_ms: greatest common divisor in milliseconds
            * m_steps: number of steps to send intra-message
            * n_steps: number of steps to send message group
        """
        gcd_ms = min(period_ms, period_intra_ms)
        while gcd_ms > 1:
            if period_ms % gcd_ms == 0 and period_intra_ms % gcd_ms == 0:
                break
            gcd_ms -= 1
        m_steps = int(period_intra_ms / gcd_ms)
        n_steps = int(period_ms / gcd_ms)
        return (gcd_ms, m_steps, n_steps)

class MultiRateCyclicSendTaskABC(CyclicSendTaskABC, abc.ABC):
    """A Cyclic send task that supports switches send frequency after a set time."""

    def __init__(
        self,
        channel: typechecking.Channel,
        messages: Union[Sequence[Message], Message],
        count: int,  # pylint: disable=unused-argument
        initial_period: float,  # pylint: disable=unused-argument
        subsequent_period: float,
    ) -> None:
        """
        Transmits a message `count` times at `initial_period` then continues to
        transmit messages at `subsequent_period`.

        :param channel: See interface specific documentation.
        :param messages:
        :param count:
        :param initial_period:
        :param subsequent_period:

        :raises ValueError: If the given messages are invalid
        """
        super().__init__(messages, subsequent_period)
        self._channel = channel


class ThreadBasedCyclicSendTask(
    LimitedDurationCyclicSendTaskABC, ModifiableCyclicTaskABC, RestartableCyclicTaskABC, VariableRateCyclicTaskABC
):
    """Fallback cyclic send task using daemon thread."""

    def __init__(
        self,
        bus: "BusABC",
        lock: threading.Lock,
        messages: Union[Sequence[Message], Message],
        period: float,
        duration: Optional[float] = None,
        on_error: Optional[Callable[[Exception], bool]] = None,
        modifier_callback: Optional[Callable[[Message], None]] = None,
        period_intra: Optional[float] = None,
    ) -> None:
        """Transmits `messages` with a `period` seconds for `duration` seconds on a `bus`.

        The `on_error` is called if any error happens on `bus` while sending `messages`.
        If `on_error` present, and returns ``False`` when invoked, thread is
        stopped immediately, otherwise, thread continuously tries to send `messages`
        ignoring errors on a `bus`. Absence of `on_error` means that thread exits immediately
        on error.

        :param on_error: The callable that accepts an exception if any
                         error happened on a `bus` while sending `messages`,
                         it shall return either ``True`` or ``False`` depending
                         on desired behaviour of `ThreadBasedCyclicSendTask`.

        :raises ValueError: If the given messages are invalid
        """
        super().__init__(messages, period, duration)
        self.bus = bus
        self.send_lock = lock
        self.stopped = True
        self.thread: Optional[threading.Thread] = None
        self.end_time: Optional[float] = (
            time.perf_counter() + duration if duration else None
        )
        self.on_error = on_error
        self.modifier_callback = modifier_callback
        self._check_and_apply_period_intra(period_intra)

        if USE_WINDOWS_EVENTS:
            if not self._is_variable_rate:
                self.period_ms = int(round(period * 1000, 0))
            try:
                self.event = win32event.CreateWaitableTimerEx(
                    None,
                    None,
                    win32event.CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                    win32event.TIMER_ALL_ACCESS,
                )
            except (AttributeError, OSError, pywintypes.error):
                self.event = win32event.CreateWaitableTimer(None, False, None)

        self.start()

    def stop(self) -> None:
        self.stopped = True
        if USE_WINDOWS_EVENTS:
            # Reset and signal any pending wait by setting the timer to 0
            win32event.SetWaitableTimer(self.event.handle, 0, 0, None, None, False)

    def start(self) -> None:
        self.stopped = False
        if self.thread is None or not self.thread.is_alive():
            name = f"Cyclic send task for 0x{self.messages[0].arbitration_id:X}"
            self.thread = threading.Thread(target=self._run, name=name)
            self.thread.daemon = True

            if USE_WINDOWS_EVENTS:
                win32event.SetWaitableTimer(
                    self.event.handle, 0, self.period_ms, None, None, False
                )

            self.thread.start()

    def _run(self) -> None:
        self.msg_index = 0
        msg_due_time_ns = time.perf_counter_ns()

        if USE_WINDOWS_EVENTS:
            # Make sure the timer is non-signaled before entering the loop
            win32event.WaitForSingleObject(self.event.handle, 0)

        while not self.stopped:
            msg_send = (self._run_cnt in self._run_cnt_msgs) if self._is_variable_rate else True
            if self.end_time is not None and time.perf_counter() >= self.end_time:
                break
            if msg_send:
                # Prevent calling bus.send from multiple threads
                with self.send_lock:
                    try:
                        if self.modifier_callback is not None:
                            self.modifier_callback(self.messages[self.msg_index])
                        self.bus.send(self.messages[self.msg_index])
                    except Exception as exc:  # pylint: disable=broad-except
                        log.exception(exc)

                        # stop if `on_error` callback was not given
                        if self.on_error is None:
                            self.stop()
                            raise exc

                        # stop if `on_error` returns False
                        if not self.on_error(exc):
                            self.stop()
                            break
                self.msg_index = (self.msg_index + 1) % self.msgs_len

            if not USE_WINDOWS_EVENTS:
                msg_due_time_ns += self.period_ns

            if USE_WINDOWS_EVENTS:
                win32event.WaitForSingleObject(
                    self.event.handle,
                    win32event.INFINITE,
                )
            else:
                # Compensate for the time it takes to send the message
                delay_ns = msg_due_time_ns - time.perf_counter_ns()
                if delay_ns > 0:
                    time.sleep(delay_ns / NANOSECONDS_IN_SECOND)
            
            if self._is_variable_rate:
                self._run_cnt = (self._run_cnt + 1) % self._run_cnt_max
