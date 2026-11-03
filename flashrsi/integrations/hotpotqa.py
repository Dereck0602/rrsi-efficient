"""Connect native harness shell tools to a user-configured Wikipedia search API."""

import copy
import json
import math
from dataclasses import replace
from urllib.request import Request, build_opener

from flashrsi._records import _redact, _secrets
from flashrsi.integrations import _web_tools
from flashrsi.integrations._browsecomp_web import _body, _http_url, _NoRedirect


def _search(config, query, top_k):
    headers = {"Content-Type": "application/json", **config.get("headers", {})}
    if config.get("api_key"):
        headers["Authorization"] = "Bearer " + config["api_key"]
    request = Request(
        config["url"], json.dumps({"query": query, "top_k": top_k}).encode(), headers
    )
    with build_opener(_NoRedirect()).open(
        request, timeout=config["timeout"]
    ) as response:
        value = json.loads(_body(response))
    results = value["results"]
    if not isinstance(results, list):
        raise ValueError("Search response requires a results list")
    public = []
    for item in results[:top_k]:
        title, sentences, ids = item["title"], item["sentences"], item["sentence_ids"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(sentences, list)
            or not isinstance(ids, list)
            or len(sentences) != len(ids)
            or len(set(ids)) != len(ids)
            or any(not isinstance(text, str) for text in sentences)
            or any(type(index) is not int or index < 0 for index in ids)
        ):
            raise ValueError(
                "Search results need titles, sentences and original sentence_ids"
            )
        # No backend-specific fields, oracle hints or credentials enter the trace.
        public.append({"title": title, "sentences": sentences, "sentence_ids": ids})
    return {"results": public}


def bind(agent, benchmark):
    _web_tools._check_target(agent, "HotpotQA fullwiki")
    config = copy.deepcopy(dict(benchmark.config.get("retrieval", {})))
    _http_url(config.get("url", ""))
    if (
        not isinstance(config.get("corpus_version"), str)
        or not config["corpus_version"].strip()
    ):
        raise ValueError("HotpotQA fullwiki requires retrieval.corpus_version")
    timeout = config.setdefault("timeout", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("retrieval.timeout must be positive and finite")
    count = config.setdefault("top_k", 10)
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("retrieval.top_k must be between 1 and 100")
    secrets = sorted(set(_secrets(config)), key=len, reverse=True)

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("HotpotQA requires a loaded benchmark Task")
        command = _web_tools._command(timeout + 5)
        prompt = (
            task.question
            + f'\n\nSearch tool: run {command} search "your query" [--top-k N] using your shell tool. Results preserve article titles and original sentence_ids. No reference answer or supporting facts are available through this tool.'
        )

        def respond(raw):
            try:
                value = json.loads(raw)
                if value["action"] != "search":
                    raise ValueError("HotpotQA fullwiki only supports search")
                query = value["query"]
                top_k = value.get("top_k")
                top_k = count if top_k is None else top_k
                if (
                    not isinstance(query, str)
                    or not query.strip()
                    or type(top_k) is not int
                    or not 1 <= top_k <= 100
                ):
                    raise ValueError(
                        "Search requires query and top_k between 1 and 100"
                    )
                return _redact(_search(config, query, top_k), secrets)
            except Exception as exc:
                return {"error": f"Search failed ({type(exc).__name__})"}

        result = await _web_tools._run_with_relay(target, prompt, respond)
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run
