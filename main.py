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

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

redis_client = redis.StrictRedis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0, decode_responses=True)

def get_db_connection():
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

limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("The app is launching")
    init_db()
    yield
    print("The app stops")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

def get_openai_response(prompt: str):
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
        print(f"An error occurred during the request to OpenAI: {e}")
        raise HTTPException(status_code=500, detail="Error in the interaction with OpenAI API")

def log_request_to_db(ip_address: str, request_text: str, response_text: str):
    connection = get_db_connection()
    if connection is None:
        print("Failed to log the request: No connection to the database.")
        return

    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("INSERT INTO request_logs (ip_address, request_text, response_text) VALUES (%s, %s, %s)"),
            [ip_address, request_text, response_text]
        )
        connection.commit()
    connection.close()

@app.get("/chat")
@limiter.limit("5/minute")
async def chat_endpoint(request: Request, query: str):
    client_ip = get_remote_address(request)
    cached_response = redis_client.get(query.lower())
    
    if cached_response:
        print(f"The answer for '{query}' found in cache")
        log_request_to_db(client_ip, query, "FROM_CACHE:" + cached_response)
        return {"response": cached_response, "source": "cache"}

    print(f"The answer for '{query}' not found in cache. Creating a request to OpenAI")
    try:
        response_text = get_openai_response(query)
    except HTTPException as e:
        log_request_to_db(client_ip, query, f"Error: {e.detail}")
        raise e

    redis_client.setex(query.lower(), 3600, response_text)
    log_request_to_db(client_ip, query, response_text)
    return {"response": response_text, "source": "api"}

@app.get("/")
def read_root():
    return {"message": "Simple chatbot. Use endpoint /chat?query=Your_request"}
