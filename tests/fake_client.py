"""
A scripted stand-in for the OpenAI client, so the agents can be tested without a model.

The agents only ever do this with a client:

    reply = client.chat.completions.create(model=..., messages=..., ...)
    text = reply.choices[0].message.content

FakeClient has exactly that shape. A test hands it a script, one entry per model
call, and each call takes the next entry:

    a string       -> returned as the reply text
    None           -> a reply with no text (content is None)
    an Exception   -> raised, as if the model server was down or timed out

    client = FakeClient(['{"reason": "ok", "on_topic": true, "claims_action": false}'])
    client = FakeClient([TimeoutError("model is down")])
    client = FakeClient(['{"a": 1}'], refuse_response_format=True)

refuse_response_format=True imitates a server that does not support a fixed
reply shape: any call that passes response_format raises (without using up a
script entry), and the same call without it works. That is the fall-back path
every agent has.

Every call is recorded in client.calls (the keyword arguments exactly as the
agent sent them), so a test can also check what the agent showed the model.
"""
from types import SimpleNamespace


class FakeServerError(Exception):
    """What the fake server raises when it refuses a request."""


class FakeClient:
    def __init__(self, replies=(), refuse_response_format=False):
        self.script = list(replies)             # what is left to answer with
        self.refuse_response_format = refuse_response_format
        self.calls = []                         # every request received, in order
        # the same nesting as the real client: client.chat.completions.create(...)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.calls.append(request)
        if self.refuse_response_format and "response_format" in request:
            raise FakeServerError("this server does not support response_format")
        if not self.script:
            raise FakeServerError("FakeClient has no scripted reply left for this call")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        message = SimpleNamespace(content=entry, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def prompt_text(self, call=-1):
        """All the text the agent sent in one call (default: the last), joined, for 'did it show X' checks."""
        return "\n".join(str(m.get("content", "")) for m in self.calls[call]["messages"])
