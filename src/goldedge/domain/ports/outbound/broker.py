from typing import Protocol, List, Optional, Dict, Any, Tuple
from ...models.trade import Position

class IBrokerAdapter(Protocol):
    def connect(self) -> bool:
        """Establishes connection to broker."""
        ...

    def reconnect_with_credentials(self, account: int, password: str, server: str, path: str = "") -> Tuple[bool, str]:
        """Dynamically switches credentials and reconnects to broker (Demo vs Live)."""
        ...

    def get_status(self) -> Dict[str, Any]:
        """Returns broker connection and trade mode status."""
        ...

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Returns live account balance, equity, margin, and leverage."""
        ...

    def place_order(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        """Places an order and returns the ticket number if successful."""
        ...
        
    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        """Closes a position entirely or partially (if volume is provided)."""
        ...
        
    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Returns currently open positions."""
        ...
        
    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        """Modifies Stop Loss or Take Profit of an existing position."""
        ...
