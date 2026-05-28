# LLM_METRIC 인제스트 프로토콜

세션 녹화 중인 phone-controller GUI는 `adb logcat -s LLM_METRIC:I` 를 항상
구독합니다. **앱은 `Log.i("LLM_METRIC", <JSON 한 줄>)` 만 호출하면** 끝.

수집된 라인은 세션 폴더의 `llm_runtime_metrics.jsonl` 에 누적되고,
세션 종료 시 `report.md` 생성에 사용됩니다.

## 표준 스키마 (전부 선택, 가지고 있는 필드만 보내도 됨)

```json
{
  "event": "load|tokenizer|prefill|decode|done",
  "ts": 0,
  "model_name": "gemma-2b-it",
  "model_size": "2.5GB",
  "quantization": "int4",
  "runtime": "MediaPipe|TFLite|LiteRT|llama.cpp|custom",
  "backend_requested": "gpu",
  "backend_actual": "gpu+cpu_fallback",
  "context_length": 2048,
  "prompt_tokens": 128,
  "output_tokens": 64,
  "load_time_ms": 1830,
  "delegate_init_ms": 820,
  "tokenizer_ms": 4,
  "ttft_ms": 612,
  "prefill_ms": 480,
  "decode_ms": 1840,
  "ms_per_token": 28.7,
  "tokens_per_sec": 34.8,
  "kv_cache_size_mb": 192,
  "fallback_count": 0,
  "cold_or_warm_run": "cold"
}
```

## Android 측 (Kotlin/Java)

```kotlin
import org.json.JSONObject

val m = JSONObject().apply {
    put("event", "done")
    put("model_name", "gemma-2b-it")
    put("backend_requested", "gpu")
    put("backend_actual", "gpu")
    put("ttft_ms", 612)
    put("tokens_per_sec", 34.8)
}
android.util.Log.i("LLM_METRIC", m.toString())
```

## 수동 테스트 (앱 없이 단발 송신)

```bash
adb shell log -t LLM_METRIC -p i '{"event":"done","tokens_per_sec":12.3,"backend_actual":"cpu"}'
```

## 권장 이벤트 순서

1. `event=load` — 모델 mmap 직후
2. `event=tokenizer` — 토크나이저 초기화 후
3. `event=prefill` — prefill 완료 시 (ttft_ms 포함)
4. `event=decode` — 일정 간격(예: 매 N 토큰)으로 ms_per_token, tokens_per_sec 갱신
5. `event=done` — 추론 종료 시 최종 집계
