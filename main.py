import os
import redis
import psycopg2
from psycopg2 import sql
import openai
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from contextlib import asynccontextmanager
from typing import Optional
#load the variables from .env file 
load_dotenv()

#setting the client for external servies

#set the API key for OpenAI 
openai.api_key = os.getenv("OPENAI_API_KEY")

#create a Redis client
#host, port and db are extracted from the environment
redis_client = redis.StrictRedis(host=os.getenv("REDIS_HOST", "localhost"),
                                port=6379, 
                                db=0, 
                                decode_responses=True)


#functions for working with the PostgreSQL database
def get_db_connection() -> Optional[psycopg2.extensions.connection]:
    """This function creates and returns the connection to the PostgreSQL.

    Returns:
        _type_: connection object is successful, otherwise None
    """
    try:
        connection = psycopg2.connect(
            dbname=os.getenv("POSTGRES_DB"),
            user=os.getenv("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD"),
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port="5432"
        )
        return connection
    except psycopg2.OperationalError as e:
        print(f"PostgreSQL Connection Error: {e}")
        return None

def init_db():
    """This function initialize the database for logging."""
    connection = get_db_connection()
    if connection is None:
        return
    
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS request_logs (
                id SERIAL PRIMARY KEY,
                ip_address VARCHAR(45) NOT NULL,
                request_text TEXT NOT NULL,
                response_text TEXT,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP 
            );
        """)
        connection.commit()
    connection.close()


#web application setup

#create a limiter instance that will use user's IP address for tracking/
limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """This function is an async context manager to manage the FastAPI application's lifespan.
    Th ecode before yield is executed on application start, and after yield
    is executed on shutdown.

    Args:
        app (FastAPI): _description_
    """
    print("The app is launching")
    init_db()
    yield
    print("The app stops")

#create a FastAPI app instance, passing the lifespan manager to it.
#set the limiter and a RateLimitExceeded error handler, when the user
#exceeds the limit
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
        #used gpt-3.5-turbo, because text-davinci-003 was depreceated  
        #https://community.openai.com/t/text-davinci-003-deprecated/582617/4
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            #messages is a list of dicts describing the conversation
            messages=[
                #system role sets the bot's behaviour
                #while user role contains a query
                {"role": "system", 
                 "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        #response text extractor
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"An error occurred during the request to OpenAI: {e}")
        raise HTTPException(status_code=500, 
                            detail="Error in interaction with OpenAI API")

def log_request_to_db(ip_address: str, request_text: str, response_text: str):
    """This function logs requests and response information to the PostgreSQL database.

    Args:
        ip_address (str): client's IP address
        request_text (str): text of the original query 
        response_text (str): response text
    """
    connection = get_db_connection()
    if connection is None:
        print("Failed to log the request: No connection to the database.")
        return

    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("INSERT INTO request_logs "
            "(ip_address, request_text, response_text) VALUES (%s, %s, %s)"),
            [ip_address, request_text, response_text]
        )
        connection.commit()
    connection.close()


#api endpoints

@app.get("/chat")
@limiter.limit("5/minute")
async def chat_endpoint(request: Request, query: str) -> dict[str, str]:
    """This function is the main chatbot endpoint that processes user requests.

    Args:
        request (Request): FastAPI request object 
        query (str): text query from the user

    Raises:
        HTTPException: raised if an error occurs during the OpenAI API call

    Returns:
        dict[str, str]: a dictionary containing the response and its source ('api' or 'cache')
    """
    client_ip = get_remote_address(request)
    #check redis cache 
    cached_response = redis_client.get(query.lower())
    
    if cached_response:
        print(f"The answer for '{query}' found in cache")
        log_request_to_db(client_ip, query, "FROM_CACHE:" + cached_response)
        return {"response": cached_response, "source": "cache"}

    print(f"The answer for '{query}' not found in cache. \
           Creating a request to OpenAI")
    
    #make the request to the OpenAI API.
    try:
        response_text = get_openai_response(query)
    except HTTPException as e:
        log_request_to_db(client_ip, query, f"Error: {e.detail}")
        raise e

    #save the response to the redis cache
    #the redis cache has expiration time - 3600 seconds
    redis_client.setex(query.lower(), 3600, response_text)

    #log request and response
    log_request_to_db(client_ip, query, response_text)
    return {"response": response_text, "source": "api"}

@app.get("/")
def read_root() -> dict[str,str]:
    """This function is a root endpoint to check that the server is running.

    Returns:
        dict[str,str]: a message shown at the start
    """
    return {"message": "Simple chatbot. Use endpoint \
             /chat?query=Your_request"}
