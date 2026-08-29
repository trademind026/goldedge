import os
import sys
import logging
from sqlalchemy import text, create_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.goldedge.config.settings import Settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("init_db")

def init_db():
    settings = Settings()
    logger.info(f"Connecting to database: {settings.DB_URL}")
    
    # We must explicitly disable transaction blocks for TimescaleDB create_hypertable
    engine = create_engine(settings.DB_URL, isolation_level="AUTOCOMMIT")
    
    migrations_dir = os.path.join(os.path.dirname(__file__), '..', 'migrations')
    
    # Get all .sql files and sort them to run in order (001, 002, etc.)
    sql_files = sorted([f for f in os.listdir(migrations_dir) if f.endswith('.sql')])
    
    if not sql_files:
        logger.warning("No .sql files found in migrations directory!")
        return

    with engine.connect() as conn:
        for sql_file in sql_files:
            file_path = os.path.join(migrations_dir, sql_file)
            logger.info(f"Executing migration: {sql_file}")
            
            with open(file_path, 'r') as f:
                sql_content = f.read()
                
            try:
                # Execute the raw SQL
                conn.execute(text(sql_content))
                logger.info(f"Successfully applied {sql_file}")
            except Exception as e:
                logger.error(f"Failed to apply {sql_file}: {e}")
                
    logger.info("Database schema initialization complete!")

if __name__ == "__main__":
    init_db()
