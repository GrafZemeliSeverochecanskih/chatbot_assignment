# Chatbot with caching and request restrictions 
This repository contains an implenatation of a simple chatbot created using Python. The bot interacts with OpenAI API to generate an answer. It uses Redis for caching, PotgreSQL for logging and has restriction of the number of requests.
The project can be deployed using Docker.

## Main Functionalities:
* Ineraction with OpenAI: Sends a text request to the OpenAI API and returns the answer from it (gpt-3.5-turbo).
* Answer Caching: Uses Redis for answer caching, which allows to avoid the API request dublication.
* Request Rate Limiting: Restricts the number of requests to 5 per minute from one IP-address
* Request Logging: Saves the history of all requests (IP-address, reqest text, answer, and time) in a PostgreSQL database

## Launching and Running
1. Clone the repository
```bash
git clone https://github.com/GrafZemeliSeverochecanskih/chatbot_assignment.git
cd chatbot_assignment
```

2. Create .env file

Create a .env in the root directory of the project. This file should not be uploaded to the repository. For the file, copy the content below and add your API key. 
```
OPENAI_API_KEY="sk-..."

POSTGRES_DB=chatbot_logs
POSTGRES_USER=user
POSTGRES_PASSWORD=password
POSTGRES_HOST=db

REDIS_HOST=redis
```

3. Run the project using Docker
Open the terminal in the project folder and run the following command to create the application image and launch three containers for database, PostgreSQL, and Redis server:
```docker-compose up --build```

Usage
After a successfull running, the application can be accessed at the address ```http://localhost:8000```:
- Main endpoint for chat: ```GET /chat```
- Parameter: query (your text query)

Example:
```
http://localhost:8000/chat?query=What is the capital of France?
```

```
Answer Example:
{
  "response": "The capital of France is Paris.",
  "source": "api"
}
```

If you try to send the same request, the source will change to "cache".

Project structure:
```
/
├── .env              
├── .gitignore        
├── docker-compose.yml
├── Dockerfile        
├── main.py           
└── requirements.txt
```