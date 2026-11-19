# Speed and tuning

Every source ends with a **timing summary and hints**. Unchanged sources are
skipped by the cache without any LLM call.

## Check the server

```bash
docpipe --ollama http://ollama.example.internal:11434 check
```

It lists the endpoints and models, measures tokens per second, and shows the
**percentage of the model in VRAM**. `--no-bench` skips the speed test.

## The usual causes

| Cause | What to do |
|---|---|
| The model isn't fully in VRAM | `docpipe check` shows it. Use a smaller model or free VRAM |
| Model reloads | Use one model for every role (leave `translator_model` and `qa_model` unset), and fewer `num_ctx_buckets` |
| QA loops | `--fast`, or `pipeline.semantic_qa: false`, or fewer `qa_rounds` |
| Too much input | Lower `pipeline.input_token_budget` (6000 keeps articles in the 8k context bucket), `max_files` or `max_chars_per_file` |

## Context buckets

Ollama reloads the model whenever `num_ctx` changes. docpipe therefore uses a
few fixed sizes instead of one per request:

```yaml
llm:
  num_ctx_buckets: [8192, 16384]      # fewer buckets = fewer model reloads
  keep_alive: 60m
  parallel_per_endpoint: 1            # set to the server's OLLAMA_NUM_PARALLEL
```

## More GPUs

List several Ollama servers in `llm.endpoints`; requests are spread over them.
Set `parallel_per_endpoint` to each server's `OLLAMA_NUM_PARALLEL`, and
`pipeline.workers` for the number of sources built at the same time.

## Fast mode

`--fast` uses priority files only, one QA round and no LLM review. Good for
previews and first drafts; `docpipe mr` is usually run with it.
