from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Type, Union
import pythoncom
import time
import threading
from queue import Queue, Empty

from comtypes import COMObject, GUID
from comtypes.automation import VARIANT, VARIANT_BOOL
from comtypes.client import CreateObject

from config.quote_types import QuoteType
from src.core.error_handler import (
    RTDClientError,
    RTDConnectionError,
    RTDConnectionState,
    RTDHeartbeatError,
    RTDServerError,
    RTDUpdateError,
    handle_com_error,
    log_method_call,
    validate_connection_state
)
from src.core.logger import get_logger
from src.core.settings import SETTINGS
from src.rtd.interfaces import IRTDUpdateEvent, IRtdServer
from src.utils import cleanup, state, topic
from src.utils.quote import Quote


class RTDClient(COMObject):
    """
    Real-Time Data Client for ThinkorSwim RTD Server.
    
    This class provides a synchronous interface to the ThinkorSwim RTD Server,
    handling real-time market data subscriptions and updates.
    
    Attributes:
        _state (RTDConnectionState): Current connection state
        server (IRtdServer): COM server instance
        topics (Dict[int, Tuple[str, str]]): Active topic subscriptions
        heartbeat_interval (int): Server heartbeat interval in milliseconds
    """
    _com_interfaces_ = [IRTDUpdateEvent]

    def __init__(
        self, 
        heartbeat_ms: Optional[int] = None,
        logger: Optional[Any] = None,
    ) -> None:
        """
        Initialize the RTD Client.

        Args:
            heartbeat_ms: Optional heartbeat interval in milliseconds.
                         Defaults to value from config.
            logger: Optional logger instance. If None, creates a new logger.
        """
        super().__init__()
        print("RTDClient initialization started")
        
        # Initialize logger
        self.logger = logger or get_logger("RTDClient")
        
        # COM server and state
        self.server: Optional[IRtdServer] = None
        self._state = RTDConnectionState.DISCONNECTED
        self._lock = Lock()
        
        # Topic management
        self.topics: Dict[int, Tuple[str, str]] = {}
        self._topic_lock = Lock()
        self._latest_values: Dict[Tuple[str, str], Quote] = {} 
        self._value_lock = Lock() 
        self._quote_listeners: list[callable] = []  
        
        # Heartbeat configuration
        self._heartbeat_interval = (
            heartbeat_ms or 
            SETTINGS['timing']['initial_heartbeat']
        )
        
        # Update tracking
        self._update_notify_count = 0
        self._last_refresh_time = None
        
        self.logger.info("RTD Client instance created")
        
        print("RTDClient __init__ completed")

    def __enter__(self) -> 'RTDClient':
        """
        Enter the runtime context for using the RTD client.
        
        Initializes the COM server and establishes the connection.
        
        Returns:
            RTDClient: Self reference for context manager use
            
        Raises:
            RTDServerError: If server initialization fails
        """
        self.initialize()
        return self
    

    @handle_com_error(RTDServerError)
    @log_method_call()
    def initialize(self) -> None:
        """
        Initialize the RTD server connection.
        
        Performs COM initialization and server startup sequence.
        Should be called before any other operations.
        
        Raises:
            RTDServerError: If server initialization fails
            RTDConnectionError: If called in invalid state
        """
        if self._state != RTDConnectionState.DISCONNECTED:
            raise RTDConnectionError(
                f"Initialization attempted in invalid state: {self._state}"
            )
            
        self._state = RTDConnectionState.CONNECTING
        self.logger.info("Starting RTD server initialization")
        print("Starting RTD server initialization")
        
        try:
            # Initialize COM for the current thread
            print("client.py about to initialize COM")
            pythoncom.CoInitialize()
            print("COM initialized")

            time.sleep(.5)  # Small delay to ensure COM is ready
            
            # Create COM server instance
            self.server = CreateObject(
                GUID(SETTINGS['rtd']['progid']), 
                interface=IRtdServer
            )
            self.logger.info("COM server instance created")
            print("COM server instance created")
            
            # Start the server
            result = self.server.ServerStart(self)
            
            if result == 1:
                self._state = RTDConnectionState.CONNECTED
                self.logger.info("Server started successfully")
                print("RTD Server started successfully")
                
                # Configure heartbeat
                current_interval = self.heartbeat_interval
                self.heartbeat_interval = SETTINGS['timing']['default_heartbeat']
                self.logger.info(
                    f"Heartbeat interval updated: {current_interval}ms -> "
                    f"{self.heartbeat_interval}ms"
                )
            else:
                raise RTDServerError(f"ServerStart failed with result: {result}")
                
        except Exception as e:
            self._state = RTDConnectionState.DISCONNECTED
            self.logger.error(f"Server initialization failed: {str(e)}")
            print(f"Server initialization failed: {str(e)}")
            cleanup.cleanup_com() 
            raise

    # The rest of the methods remain unchanged
    @handle_com_error(RTDClientError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED])
    def subscribe(self, quote_type: Union[str, QuoteType], symbol: str) -> Optional[int]:
        with self._topic_lock:
            quote_type_str = topic.validate_quote_type(quote_type)
            topic_id = topic.generate_topic_id(quote_type_str, symbol)
            
            if topic_id in self.topics:
                self.logger.info(
                    f"Already subscribed to {symbol} {quote_type_str}"
                )
                return topic_id
                
            strings = (VARIANT * 2)()
            strings[0].value = quote_type_str
            strings[1].value = symbol
            get_new_values = VARIANT_BOOL(True)
            
            try:
                result = self.server.ConnectData(
                    topic_id, strings, get_new_values
                )
                self.logger.debug(f"Subscription raw result {result}")
                
                if isinstance(result, list) and len(result) >= 1 and result[0]:
                    self.topics[topic_id] = (symbol, quote_type_str)
                    self.logger.debug(
                        f"Subscribed to {symbol} {quote_type_str} "
                        f"with ID {topic_id}"
                    )
                    return topic_id
                else:
                    self.logger.warning(
                        f"Subscription failed for {symbol} {quote_type_str}"
                    )
                    return None
                    
            except Exception as e:
                self.logger.error(
                    f"Error subscribing to {symbol} {quote_type_str}: {e}"
                )
                raise RTDClientError(
                    f"Subscription failed for {symbol}"
                ) from e

    @handle_com_error(RTDClientError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED, RTDConnectionState.DISCONNECTING])
    def unsubscribe(self, quote_type: Union[str, QuoteType], symbol: str) -> bool:
        with self._topic_lock:
            quote_type_str = topic.validate_quote_type(quote_type)
            
            topic_id = topic.find_topic_id(self.topics, symbol, quote_type_str)
            if topic_id is None:
                self.logger.warning(
                    f"Not subscribed to {symbol} {quote_type_str}"
                )
                return False
                
            try:
                result = self.server.DisconnectData(topic_id)
                self.logger.debug(f"Unsub raw result {result}")
                
                if result == 0:  # Success
                    del self.topics[topic_id]
                    self.logger.debug(
                        f"Unsubscribed from {symbol} {quote_type_str}"
                    )
                    return True
                else:
                    self.logger.warning(
                        f"Unsubscription failed for {symbol} {quote_type_str}"
                    )
                    return False
                    
            except Exception as e:
                self.logger.error(
                    f"Error unsubscribing from {symbol} {quote_type_str}: {e}"
                )
                return False


    @handle_com_error(RTDUpdateError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED])
    def UpdateNotify(self) -> bool:
        self._update_notify_count += 1
        # Only log occasionally to reduce noise
        if self._update_notify_count == 1 or self._update_notify_count % 100 == 0:
            self.logger.debug(f"UpdateNotify called (count: {self._update_notify_count})")
            print(f"Update notification #{self._update_notify_count}")
        return self.refresh_topics()

    @handle_com_error(RTDClientError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED])
    def refresh_topics(self) -> bool:
        try:
            result = self.server.RefreshData()
            self._last_refresh_time = time.time()
            
            if not result or not isinstance(result, list) or len(result) != 2:
                self.logger.warning(f"Unexpected result format from RefreshData: {result}")
                return False

            topic_count, data = result
            
            # Only log meaningful updates
            if topic_count > 0 and self._update_notify_count % 100 == 0:
                print(f"Refresh data for {topic_count} topics")
                
            if topic_count == 0 or not data:
                return True

            if isinstance(data, tuple) and len(data) == 2:
                topic_ids, raw_values = data
                for id, raw_value in zip(topic_ids, raw_values):
                    if id in self.topics:
                        symbol, quote_type = self.topics[id]
                        quote_obj = Quote(quote_type, symbol, raw_value)
                        #print("refresh topics called. it now calls handle_quote_update")
                        self._handle_quote_update(id, symbol, quote_type, quote_obj)
                return True
            else:
                self.logger.warning(f"Unexpected data format in RefreshData result: {data}")
                return False
           
        except Exception as e:
            self.logger.error(f"Error fetching or processing refresh data: {e}", exc_info=True)
            print(f"ERROR in refresh_topics: {str(e)}")
            return False

    # ---------------------------------------------------------------------
    #   public helper
    # ---------------------------------------------------------------------
    def add_quote_listener(self, callback: callable) -> None:
        """
        Register a callback that will be invoked for every Quote object
        the client receives.  Signature:  callback(quotes: list[Quote])
        (We wrap single quotes in a list.)
        """
        if not callable(callback):
            raise ValueError("listener must be callable")
        self._quote_listeners.append(callback)


    def _handle_quote_update(self, id: int, symbol: str, quote_type: str, quote: Quote) -> None:
        """
        Handle incoming quote updates without checking for value changes.
        This ensures persistent quotes (like market maker quotes) stay active
        even if they don't change.
        """
        try:
            if quote.value is None:
                self.logger.debug(f"Null value received for {symbol} {quote_type}")
                return

            # Update latest value without checking if changed
            with self._value_lock:
                key = (symbol, quote_type)
                self._latest_values[key] = quote

            # Log first quote or occasional quotes for debugging
            if self._update_notify_count < 5 or self._update_notify_count % 500 == 0:
                print(f"Quote: {symbol} {quote_type}: {quote.value}")
            
            # Notify listeners
            for listener in self._quote_listeners:
                try:                    
                    self.logger.debug(f"Calling listener with quote - Symbol: {quote.symbol}, Type: {quote.quote_type}, Value: {quote.value}, Timestamp: {quote.timestamp:.3f}")
                    listener([quote])
                except Exception as e:
                    self.logger.error(f"Quote listener error: {str(e)}")
                    self.logger.error(f"Failed quote details - Symbol: {quote.symbol}, Type: {quote.quote_type}, Value: {quote.value}, Timestamp: {quote.timestamp:.3f}")

        except Exception as e:
            self.logger.error(f"Error handling quote update: {e}")
            print(f"Error in _handle_quote_update: {str(e)}")

    @handle_com_error(RTDHeartbeatError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED, RTDConnectionState.DISCONNECTED])
    def check_heartbeat(self) -> bool:
        if self._state == RTDConnectionState.DISCONNECTED:
            self.logger.debug("Heartbeat check skipped - disconnected state")
            return False
            
        try:
            result = self.server.Heartbeat()
            is_healthy = result == 1
            
            if not is_healthy:
                self.logger.warning(
                    f"Unhealthy heartbeat response: {result}"
                )
            
            return is_healthy
            
        except Exception as e:
            self.logger.error(f"Heartbeat check failed: {e}")
            raise RTDHeartbeatError("Heartbeat operation failed") from e

    @property
    def heartbeat_interval(self) -> int:
        return self._heartbeat_interval

    @heartbeat_interval.setter
    def heartbeat_interval(self, interval: int) -> None:
        if interval <= 0:
            raise ValueError("Heartbeat interval must be positive")
            
        self._heartbeat_interval = interval
        self.logger.info(f"Heartbeat interval set to {interval}ms")

    @handle_com_error(RTDServerError)
    @log_method_call()
    @validate_connection_state([RTDConnectionState.CONNECTED, RTDConnectionState.CONNECTING])
    def Disconnect(self) -> None:
        with self._lock:
            if self._state == RTDConnectionState.DISCONNECTED:
                self.logger.info("Already disconnected")
                return
                
            if self._state == RTDConnectionState.DISCONNECTING:
                self.logger.info("Disconnect already in progress")
                return
                
            self._state = RTDConnectionState.DISCONNECTING
            self.logger.info("Starting disconnect sequence")
            
            try:
                    
                # Unsubscribe but can be optional as Excel doesn't seem to do it or not
                # very effectively for large number of topics
                subscriptions = [(qt, sym) for sym, qt in self.topics.values()]
                if subscriptions:
                    unsubscribe_results = self.batch_unsubscribe(subscriptions)
                    
                # Clear any remaining topics from memory
                cleanup.cleanup_topics(self.topics)
                
                if self.server is not None:
                    try:
                        self.server.ServerTerminate()
                        self.logger.info("Server terminated")
                    except Exception as e:
                        self.logger.error(f"Error terminating server: {e}")
                    finally:
                        self.server = None
                
                cleanup.cleanup_com()
                self._state = RTDConnectionState.DISCONNECTED
                self.logger.info("Disconnect completed")
                
            except Exception as e:
                self.logger.error(f"Error during disconnect: {e}")
                raise

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any]
    ) -> None:
        try:
            if exc_type is not None:
                self.logger.error(f"Context exit due to error: {exc_val}")
            self.Disconnect()
        except Exception as e:
            self.logger.error(f"Error during context exit: {e}")
            if exc_type is None:
                raise

    def batch_subscribe(
        self,
        subscriptions: List[Tuple[Union[str, QuoteType], str]]
    ) -> Dict[Tuple[str, str], bool]:
        results = {}
        for quote_type, symbol in subscriptions:
            try:
                topic_id = self.subscribe(quote_type, symbol)
                results[(str(quote_type), symbol)] = topic_id is not None
            except Exception as e:
                self.logger.error(
                    f"Error in batch subscribe for {symbol} {quote_type}: {e}"
                )
                results[(str(quote_type), symbol)] = False
                
        successful = sum(1 for result in results.values() if result)
        self.logger.info(
            f"Batch subscribe completed: {successful}/{len(subscriptions)} "
            "successful"
        )
        return results

    def batch_unsubscribe(
        self,
        subscriptions: List[Tuple[Union[str, QuoteType], str]]
    ) -> Dict[Tuple[str, str], bool]:
        results = {}
        for quote_type, symbol in subscriptions:
            try:
                success = self.unsubscribe(quote_type, symbol)
                results[(str(quote_type), symbol)] = success
            except Exception as e:
                self.logger.error(
                    f"Error in batch unsubscribe for {symbol} {quote_type}: {e}"
                )
                results[(str(quote_type), symbol)] = False
                
        successful = sum(1 for result in results.values() if result)
        self.logger.info(
            f"Batch unsubscribe completed: {successful}/{len(subscriptions)} "
            "successful"
        )
        return results
    
    def add_option_contract(self, base_symbol: str, exchanges: list[str] | None = None) -> bool:
        """
        Subscribe to one option contract (plus its exchange suffices) without
        touching existing subscriptions.
        """
        try:
            qt_all  = [QuoteType.BID, QuoteType.ASK,
                    QuoteType.BID_SIZE, QuoteType.ASK_SIZE,
                    QuoteType.LAST, QuoteType.LAST_SIZE]

            # base symbol first
            for qt in qt_all:
                self.subscribe(qt, base_symbol)

            if exchanges:
                qt_depth = [QuoteType.BID, QuoteType.ASK,
                            QuoteType.BID_SIZE, QuoteType.ASK_SIZE]
                for ex in exchanges:
                    sym_ex = f"{base_symbol}&{ex}"
                    for qt in qt_depth:
                        self.subscribe(qt, sym_ex)

            return True
        except Exception as e:
            self.logger.error(f"add_option_contract failed for {base_symbol}: {e}")
            return False
        # -------------------------------------------------------------------

    def __str__(self) -> str:
        #status = "Connected" if self._state == RTDConnectionState.CONNECTED else "Disconnected"
        status = "Connected" if self.is_connected else "Disconnected"
        topic_count = len(self.topics)
        return (
            f"RTDClient: {status}, "
            f"Topics: {topic_count}, "
            f"Updates: {self._update_notify_count}"
        )

    def __repr__(self) -> str:
        return (
            f"RTDClient(state={self._state.name}, "
            f"topics={len(self.topics)}, "
            f"heartbeat={self._heartbeat_interval}ms, "
            f"updates={self._update_notify_count})"
        )