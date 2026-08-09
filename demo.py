import os
import sqlite3

from langchain_deepseek import ChatDeepSeek
from langchain_core.tools import tool
from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage


from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file


@tool(description="Get the current weather for a given city.")
def get_weather(city: str) -> str:
    """
    Get the current weather for a given city.

    Args:
        city (str): The name of the city.
    Returns:
        str: A string describing the current weather in the city.

    """
    # This is a placeholder implementation. In a real-world scenario, you would call a weather API.
    return f"The current weather in {city} is sunny with a temperature of 25°C."

@tool(description="Send an email to a specified recipient.")
def send_email(recipient: str, subject: str, body: str) -> str:
    """
    Send an email to a specified recipient.

    Args:
        recipient (str): The email address of the recipient.
        subject (str): The subject of the email.
        body (str): The body content of the email.

    Returns:
        str: A confirmation message indicating that the email has been sent.
    """
    # This is a placeholder implementation. In a real-world scenario, you would integrate with an email service.
    return f"Email sent to {recipient} with subject '{subject}' and body '{body}'."

def create_checkpoint() -> SqliteSaver:
    """
    Create a checkpoint for the agent's state.

    Returns:
        SqliteSaver: A checkpoint saver backed by a SQLite database.
    """
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/checkpoint.db", check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return checkpointer

model = ChatDeepSeek(model_name="deepseek-v4-flash", temperature=0.1, max_tokens=1024)


agent = create_agent(model=model,
                     tools=[get_weather, send_email],
                     checkpointer=create_checkpoint(),
                     middleware=[
                         HumanInTheLoopMiddleware(
                         interrupt_on={"get_weather":{"allowed_decisions":["approve", "reject"]}, "send_email":False},
                        )
                     ]
                     )

message=[
    SystemMessage(content="You are a helpful assistant."),
    HumanMessage(content="Get the current weather for New York City and send an email to john@example.com if it is ok to travel."),
    ]

config = {"configurable": {"thread_id": "demo_004"}}

res = agent.invoke({"messages": message}, config=config)

# If the agent paused for human approval, prompt the user to decide.
if res.get("__interrupt__"):
    for interrupt in res["__interrupt__"]:
        request = interrupt.value
        print("\n=== Human approval required ===")
        decisions = []
        for req, cfg in zip(request["action_requests"], request["review_configs"]):
            print(f"Action: {req['name']}")
            print(f"Args:   {req['args']}")
            if req.get("description"):
                print(f"Desc:   {req['description']}")
            print(f"Allowed decisions: {cfg['allowed_decisions']}")
            while True:
                choice = input("Your decision (approve/reject): ").strip().lower()
                if choice in ("approve", "reject"):
                    break
                print("Invalid choice, please enter 'approve' or 'reject'.")
            decisions.append({"type": choice})
        res = agent.invoke(Command(resume={"decisions": decisions}), config=config)

for r in res["messages"]:
    if isinstance(r, ToolMessage):
        print(f"Tool: {r.name}, Result: {r.content}")
    elif isinstance(r, AIMessage):
        print(f"AI: {r.content}")
    elif isinstance(r, HumanMessage):
        print(f"Human: {r.content}")
    elif isinstance(r, SystemMessage):
        print(f"System: {r.content}")