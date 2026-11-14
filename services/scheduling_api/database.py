"""PostgreSQL connection provider; transactions are owned by API operations."""

import os
from contextlib import closing


def get_connection():
    import psycopg2

    with closing(psycopg2.connect(os.environ["DB_DSN"])) as connection:
        yield connection
