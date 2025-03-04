from dapr_agents import tool, ReActAgent
from dotenv import load_dotenv

load_dotenv()
@tool
def search_weather(city: str) -> str:
    """Get weather information for a city."""
    weather_data = {"london": "Rainy", "paris": "Sunny"}
    return weather_data.get(city.lower(), "Unknown")

@tool
def get_activities(weather: str) -> str:
    """Get activity recommendations."""
    activities = {"Rainy": "Visit museums", "Sunny": "Go hiking"}
    return activities.get(weather, "Stay comfortable")

react_agent = ReActAgent(
    name="TravelAgent",
    role="Travel Assistant",
    pattern="react",
    instructions=["Check weather, then suggest activities"],
    tools=[search_weather, get_activities]
)

react_agent.run("What should I do in London today?")