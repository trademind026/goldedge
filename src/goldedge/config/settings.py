import os
from typing import List, Dict
from pydantic_settings import BaseSettings
from pydantic import Field
from ..domain.models.symbol_profile import SymbolProfile

class Settings(BaseSettings):
    """
    Application-wide settings managed by Pydantic.
    Loads from environment variables, .env file, or defaults.
    """
    
    # --- Environment ---
    ENV: str = Field("development", description="development, staging, or production")
    TRADING_MODE: str = Field("demo", description="backtest, demo, or live")
    BROKER_TYPE: str = Field("ctrader", description="ctrader or mt5")
    
    # --- Database ---
    DB_URL: str = Field(
        "postgresql://user:password@localhost:5432/goldedge",
        description="TimescaleDB connection string"
    )
    
    # --- Redis (Event Bus) ---
    REDIS_URL: str = Field(
        "redis://localhost:6379/0",
        description="Redis connection string for distributed Event Bus"
    )
    
    # --- cTrader ---
    CTRADER_CLIENT_ID: str = Field("mock_client_id", description="cTrader Client ID")
    CTRADER_CLIENT_SECRET: str = Field("mock_client_secret", description="cTrader Client Secret")
    CTRADER_ACCOUNT_ID: str = Field("mock_account_id", description="cTrader Account ID")
    CTRADER_ACCESS_TOKEN: str = Field("mock_token", description="cTrader OAuth Access Token")
    CTRADER_REFRESH_TOKEN: str = Field("mock_refresh_token", description="cTrader OAuth Refresh Token")
    CTRADER_TOKEN_EXPIRES_AT: float = Field(9999999999.0, description="cTrader Token Expiry Unix Timestamp")

    # --- MetaTrader 5 ---
    MT5_ACCOUNT: int = Field(0, description="MT5 Account Number")
    MT5_PASSWORD: str = Field("", description="MT5 Password")
    MT5_SERVER: str = Field("", description="MT5 Server Name")
    MT5_PATH: str = Field(r"C:\Program Files\MetaTrader 5\terminal64.exe", description="Path to MT5 executable")
    
    # --- Trading Parameters ---
    ACTIVE_SYMBOLS: List[str] = Field(
        default_factory=lambda: ["XAUUSD"], 
        description="List of symbols the bot will actively trade and scan"
    )
    
    # --- Symbol Fallback Configuration ---
    # Used when broker is disconnected (Backtest mode)
    SYMBOL_PROFILES_FALLBACK: Dict[str, SymbolProfile] = {
        "XAUUSD": SymbolProfile(
            symbol="XAUUSD",
            contract_size=100.0,
            digits=2,
            point=0.01,
            tick_size=0.01,
            tick_value=1.0,
            min_volume=0.01,
            max_volume=100.0,
            volume_step=0.01,
            currency_base="XAU",
            currency_profit="USD",
            currency_margin="USD"
        ),
        "EURUSD": SymbolProfile(
            symbol="EURUSD",
            contract_size=100000.0,
            digits=5,
            point=0.00001,
            tick_size=0.00001,
            tick_value=1.0,  # $1 per micro-pip per standard lot
            min_volume=0.01,
            max_volume=100.0,
            volume_step=0.01,
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="EUR"
        )
    }

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"

# Global singleton
settings = Settings()
