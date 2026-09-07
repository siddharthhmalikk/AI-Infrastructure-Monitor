import os
import time
import psutil
from openai import OpenAI


client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"]
)


CPU_LIMIT = 80
RAM_LIMIT = 80
DISK_LIMIT = 90


def get_system_stats():
    return {
        "cpu": psutil.cpu_percent(interval=1),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("C:\\").percent,
    }


def get_top_processes():
    processes = []

    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            processes.append({
                "name": process.info["name"],
                "cpu": process.info["cpu_percent"],
                "memory": round(process.info["memory_percent"], 2)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    processes.sort(key=lambda x: x["memory"], reverse=True)

    return processes[:5]


def analyze_incident(stats, processes):

    process_info = "\n".join(
        [
            f"{p['name']} | CPU: {p['cpu']}% | RAM: {p['memory']}%"
            for p in processes
        ]
    )

    prompt = f"""
You are an IT infrastructure support assistant.

Analyze this Windows system incident.

SYSTEM STATUS:
CPU Usage: {stats['cpu']}%
RAM Usage: {stats['ram']}%
Disk Usage: {stats['disk']}%

TOP RESOURCE-CONSUMING PROCESSES:
{process_info}

Give a short incident report with exactly these sections:

Severity:
Likely Cause:
Recommended Action:

Keep the response practical and concise.
"""

    response = client.chat.completions.create(
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2,
        max_tokens=300
    )

    return response.choices[0].message.content


while True:

    stats = get_system_stats()
    processes = get_top_processes()

    print("\n--- SYSTEM STATUS ---")
    print(f"CPU Usage : {stats['cpu']}%")
    print(f"RAM Usage : {stats['ram']}%")
    print(f"Disk Usage: {stats['disk']}%")

    print("\n--- TOP PROCESSES ---")

    for process in processes:
        print(
            f"{process['name']} | "
            f"CPU: {process['cpu']}% | "
            f"RAM: {process['memory']}%"
        )

    incident_detected = (
        stats["cpu"] >= CPU_LIMIT
        or stats["ram"] >= RAM_LIMIT
        or stats["disk"] >= DISK_LIMIT
    )

    if incident_detected:

        print("\nALERT: High resource usage detected.")
        print("Sending incident to NVIDIA Nemotron...\n")

        try:
            analysis = analyze_incident(stats, processes)

            print("--- AI INCIDENT ANALYSIS ---")
            print(analysis)

        except Exception as error:
            print("AI analysis failed:")
            print(error)

        time.sleep(20)

    else:

        print("\nStatus: Normal")
        time.sleep(5)