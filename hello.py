import os
from openai import OpenAI

client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=os.environ["GEMINI_API_KEY"],
)
reply = client.chat.completions.create(
    model="gemini-3.8-flash",
    messages=[{"role": "user", "content": "Reply with the single word: ready"}],
)
print(reply.choices[0].message.content)