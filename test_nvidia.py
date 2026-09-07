import os
from openai import OpenAI

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"]
)

response = client.chat.completions.create(
    model="nvidia/nemotron-3.5-lightning-30b-a3b",
    messages=[
        {
            "role": "user",
            "content": "Explain what a Linux server is in simple words."
        }
    ],
    temperature=0.7,
    max_tokens=500
)

print(response.choices[0].message.content)
import psutil
import time

def get_system_stats():
    return {
        "cpu": psutil.cpu_percent(interval=1),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent,
    }

while True:
    stats = get_system_stats()

    print("\n--- System Status ---")
    print(f"CPU Usage : {stats['cpu']}%")
    print(f"RAM Usage : {stats['ram']}%")
    print(f"Disk Usage: {stats['disk']}%")

    time.sleep(3)