from dotenv import load_dotenv
from google import genai
from google.genai.types import HttpOptions

# Loads project ID, location, and platform configuration from .env
load_dotenv()

# Authentication is automatically provided by ADC.
client = genai.Client(
    http_options=HttpOptions(api_version="v1")
)

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=(
        "You are an AI fleet recovery planner. "
        "Robot-1 failed during delivery task-101. "
        "Robot-2 is available and has 80 percent battery. "
        "What should the fleet controller do?"
    ),
)

print(response.text)

