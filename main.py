import os
import redis
import psycopg2
import logging
import openai
from psycopg2 import sql
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from fastapi import FastAPI, Request, HTTPException, Depends
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from contextlib import asynccontextmanager
from typing import Generator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

#setting the client for external servies
class Settings(BaseSettings):
    """Manages all application settings using Pydantic.

    This class automatically reads environment variables and values from a .env file,
    providing a single, type-safe source for configuration.
    

    Args:
        BaseSettings (_type_): a class from the Pydantic 
        library that manages application's configuration settings
    """
    openai_api_key: str
    redis_host: str = "localhost"
    redis_port: int = 6379
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    class Config:
        env_file = ".env"

settings = Settings()

#setting the key for OpenAI API
openai.api_key = settings.openai_api_key

#create a Redis client
redis_client = redis.StrictRedis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=0,
    decode_responses=True
)

#functions for working with the PostgreSQL database
def get_db() -> Generator[psycopg2.extensions.connection, None, None]:
    """This function initializes a FastAPI dependency that yields a database
    connection for the duration of a request.
    

    Raises:
        HTTPException: raised with status 503 if the database is unavailable

    Yields:
        Generator[psycopg2.extensions.connection, None, None]: a database
        connection object
    """
    connection = None
    try:
        connection = psycopg2.connect(
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port
        )
        yield connection
    except psycopg2.OperationalError as e:
        logging.error(f"Database connection could not be established: {e}")
        raise HTTPException(status_code=503, detail="Database \
                            connection unavailable.")
    finally:
        if connection:
            connection.close()

def init_db():
    """
    This function initializes the database.
    """
    connection = None
    try:
        logging.info("Initializing database")
        connection = psycopg2.connect(
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port
        )
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id SERIAL PRIMARY KEY,
                    ip_address VARCHAR(45) NOT NULL,
                    request_text TEXT NOT NULL,
                    response_text TEXT,
                    status VARCHAR(20) NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE DEFAULT \
                           CURRENT_TIMESTAMP
                );
            """)
        connection.commit()
        logging.info("Database initialized successfully.")
    except psycopg2.OperationalError as e:
        logging.error(f"Failed to initialize database: {e}")
    finally:
        if connection:
            connection.close()

def log_request_to_db(
        db_conn: psycopg2.extensions.connection, 
        ip_address: str, 
        request_text: str, 
        response_text: str, 
        status: str
        ):
    """
    This function logs request and response information to the database
    using the provided connection.

    Args:
        db_conn (psycopg2.extensions.connection): the database connection 
        from the dependency
        ip_address (str): client's IP address
        request_text (str): text of the original query
        response_text (str): response text
        status (str): outcome of the request
    """
    with db_conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("""
                INSERT INTO request_logs (ip_address, request_text,
                     response_text, status) 
                VALUES (%s, %s, %s, %s)
            """),
            [ip_address, request_text, response_text, status]
        )
        db_conn.commit()

#web application setup

#create a limiter instance that will use user's IP address for tracking
limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages application startup and shutdown events.

    Args:
        app (FastAPI): the FastAPI application instance
    """
    logging.info("The app is launching")
    init_db()
    yield
    logging.info("The app is stopping")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

#main functionality
def get_openai_response(prompt: str) -> str:
    """This function sends a request to OpenAI API and returns a text 
    response.

    Args:
        prompt (str): the text query from user

    Raises:
        HTTPException: raised if an error occurs while interacting with the OpenAI API

    Returns:
        str: the text response from the model 
    """
    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"An error occurred during the request to OpenAI: {e}")
        raise HTTPException(status_code=500, detail="Error in interaction with OpenAI API")

#api endpoints
@app.get("/chat")
@limiter.limit("5/minute")
async def chat_endpoint(
    request: Request, 
    query: str, 
    db_conn: psycopg2.extensions.connection = Depends(get_db)
    ) -> dict[str, str]:
    """This function is the main chatbot endpoint that processes user
    requests.

    Args:
        request (Request): FastAPI request object 
        query (str): text query from the user
        db_conn (psycopg2.extensions.connection, optional): database
        connection injected by FastAPI

    Raises:
        HTTPException: raised if an error occurs during the OpenAI API call

    Returns:
        dict[str, str]: a dictionary containing the response and its 
        source ('api' or 'cache')
    """
    client_ip = get_remote_address(request)
    cached_response = redis_client.get(query.lower())
    
    if cached_response:
        logging.info(f"Answer for '{query}' found in cache.")
        log_request_to_db(db_conn, client_ip, query, cached_response, "cached")
        return {"response": cached_response, "source": "cache"}

    logging.info(f"Answer for '{query}' not in cache. Requesting from OpenAI.")
    try:
        response_text = get_openai_response(query)
        redis_client.setex(query.lower(), 3600, response_text)
        log_request_to_db(db_conn, client_ip, query, response_text, "success")
        return {"response": response_text, "source": "api"}
    except HTTPException as e:
        log_request_to_db(db_conn, client_ip, query, f"Error: {e.detail}", "error")
        raise e

@app.get("/")
def read_root() -> dict[str, str]:
    """Root endpoint to check that the server is running.

    Returns:
        dict[str, str]: a message shown at the start
    """
    return {"message": "Simple chatbot. Use endpoint /chat?query=Your_request"}