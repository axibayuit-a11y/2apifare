# 修复空响应问题（2025-12-05）

## 问题描述

从用户反馈和日志分析发现，更新后空响应情况反而更严重了。

### 问题表现

```
[ERROR] Google API returned status 404 (STREAMING). Response details: {
  "error": {
    "code": 404,
    "message": "Requested entity was not found.",
    "status": "NOT_FOUND"
  }
}

[INFO] Anti-truncation: No [done] marker found in output (length: 0), preparing continuation (attempt 2)
[ERROR] Google API returned status 404 (STREAMING) ...
[INFO] Anti-truncation: No [done] marker found in output (length: 0), preparing continuation (attempt 3)
[ERROR] Google API returned status 404 (STREAMING) ...
[WARNING] Anti-truncation: Max attempts reached, ending stream
[INFO] ... "POST /v1/chat/completions 1.1" 200 - ...  ← 注意：200 但无内容
```

### 根本原因

**抗截断机制误判了 API 错误响应！**

1. **API 返回 404/429/403 等错误**
2. 错误处理代码返回 `StreamingResponse`（错误的格式）
3. **抗截断机制检测到流式响应**，但没有 `[done]` 标记
4. 误判为"内容被截断"，尝试续写
5. 连续重试 3 次，每次都是同样的错误
6. **最终返回空内容**：`200 -`（状态码 200 但无响应体）

### 为什么没有执行重试逻辑？

代码中**已经实现了** 404 错误先刷新 token 的逻辑：

```python
# google_chat_api.py:388-408
if resp.status_code in (400, 401, 404):
    log.warning(f"[AUTH REFRESH] {resp.status_code} error, attempting token refresh...")
    refresh_success = await credential_manager.force_refresh_current_token()

    if refresh_success:
        log.info("[AUTH REFRESH] Token refreshed, retrying with same credential")
        continue  # 重试

    log.warning("[AUTH REFRESH] Token refresh failed, proceeding with credential ban")
```

但从日志看**没有执行**这段代码，因为：
- 抗截断机制**先于**重试逻辑执行
- 抗截断拦截了错误响应，自行重试了 3 次
- 外层的重试逻辑（包括刷新 token、切换凭证）根本没机会运行

## 解决方案

**修改错误响应的返回格式：使用普通 `Response` 而不是 `StreamingResponse`**

这样抗截断机制就不会介入，让正常的重试逻辑能够执行。

### 修复位置

修改了 `src/google_chat_api.py` 中的 **3 处**错误响应返回：

#### 1. 流式请求 - 非 200 状态码错误（第 624-657 行）

**修复前**：
```python
async def cleanup_and_error():
    # ... 错误处理代码 ...
    error_response = {
        "error": {
            "message": f"API error: {resp.status_code}",
            "type": "api_error",
            "code": resp.status_code,
        }
    }
    yield f"data: {json.dumps(error_response)}\n\n".encode("utf-8")

return StreamingResponse(
    cleanup_and_error(),
    media_type="text/event-stream",
    status_code=resp.status_code
)  # ❌ 会触发抗截断！
```

**修复后**：
```python
await _handle_api_error(credential_manager, resp.status_code, response_content, current_file)

# [FIX] 返回普通 Response 而不是 StreamingResponse
# 避免抗截断机制误判错误响应为需要续写的截断内容
error_response = {
    "error": {
        "message": f"API error: {resp.status_code}",
        "type": "api_error",
        "code": resp.status_code,
    }
}
return Response(
    content=json.dumps(error_response),
    status_code=resp.status_code,
    media_type="application/json",
)  # ✅ 不会触发抗截断
```

#### 2. 流式请求 - 429 错误达到最大重试（第 327-331 行）

**修复前**：
```python
else:
    # 返回429错误流
    async def error_stream():
        error_response = {
            "error": {
                "message": "429 rate limit exceeded, max retries reached",
                "type": "api_error",
                "code": 429,
            }
        }
        yield f"data: {json.dumps(error_response)}\n\n"

    return StreamingResponse(
        error_stream(),
        media_type="text/event-stream",
        status_code=429
    )  # ❌ 会触发抗截断！
```

**修复后**：
```python
else:
    # [FIX] 返回普通 Response 而不是 StreamingResponse
    # 避免抗截断机制误判错误响应为需要续写的截断内容
    return _create_error_response(
        "429 rate limit exceeded, max retries reached", 429
    )  # ✅ 不会触发抗截断
```

#### 3. 流式请求 - 其他错误达到最大重试（第 424-428 行）

**修复前**：
```python
# 返回错误流
async def error_stream():
    error_response = {
        "error": {
            "message": f"API error: {resp.status_code}",
            "type": "api_error",
            "code": resp.status_code,
        }
    }
    yield f"data: {json.dumps(error_response)}\n\n"

return StreamingResponse(
    error_stream(),
    media_type="text/event-stream",
    status_code=resp.status_code,
)  # ❌ 会触发抗截断！
```

**修复后**：
```python
# [FIX] 返回普通 Response 而不是 StreamingResponse
# 避免抗截断机制误判错误响应为需要续写的截断内容
return _create_error_response(
    f"API error: {resp.status_code}", resp.status_code
)  # ✅ 不会触发抗截断
```

## 修复效果

### 修复前的流程：
```
请求 → 404 错误 → 返回 StreamingResponse(错误)
                      ↓
              抗截断机制检测到流式响应
                      ↓
              检测到没有 [done] 标记 (length: 0)
                      ↓
              误判为"截断"，重试 3 次（都是 404）
                      ↓
              最终返回空响应（200 -）
```

### 修复后的流程：
```
请求 → 404 错误 → 返回普通 Response(错误)
                      ↓
              抗截断不介入（因为不是 StreamingResponse）
                      ↓
              外层重试逻辑执行
                      ↓
              尝试刷新 token
                      ↓
              刷新成功 → 使用同一凭证重试
              刷新失败 → 禁用凭证并切换到下一个
                      ↓
              重试最多 5 次
                      ↓
              成功 or 返回明确错误（不是空响应）
```

### 预期日志（修复后）

#### 场景 1：Token 刷新成功
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[AUTH REFRESH] 404 error, attempting token refresh before retry (1/5)
[INFO] Token刷新成功并已保存: gemini-key-xxx.json
[AUTH REFRESH] Token refreshed, retrying with same credential
[INFO] Request succeeded with same credential
```

#### 场景 2：Token 刷新失败，切换账号
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[AUTH REFRESH] 404 error, attempting token refresh before retry (1/5)
[WARNING] [AUTH REFRESH] Token refresh failed, proceeding with credential ban
[RETRY] 404 error encountered, rotating credential and retrying (1/5)
[INFO] Rotated to credential index 1
[INFO] Using credential: another-account@gmail.com
[INFO] Request succeeded with different credential
```

#### 场景 3：所有凭证都失败
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[AUTH REFRESH] 404 error, attempting token refresh...
[WARNING] Token refresh failed
[RETRY] Rotating credential...
... (重试 5 次)
[ERROR] Max retries reached
返回明确的错误响应（不是空响应）：
{
  "error": {
    "message": "API error: 404",
    "type": "api_error",
    "code": 404
  }
}
```

## 相关文件

- **修改**：`src/google_chat_api.py`（3 处修改）
- **不变**：`src/anti_truncation.py`（抗截断机制本身没问题）
- **参考**：`docs/FIX_404_EMPTY_RESPONSE.md`（之前的 404 修复文档）

## 测试建议

1. **测试 404 错误**：
   - 使用一个无权限的账号请求模型
   - 观察是否出现 `[AUTH REFRESH]` 日志
   - 确认会自动切换凭证重试

2. **测试 429 错误**：
   - 触发配额限制
   - 观察是否正确切换凭证
   - 确认不会返回空响应

3. **测试抗截断**：
   - 使用正常的长输出请求
   - 确认抗截断机制仍然正常工作
   - 只有成功的流式响应才触发抗截断

## 总结

这次修复的核心思想：

**错误响应不应该使用流式格式（StreamingResponse），应该使用普通格式（Response）**

原因：
1. 抗截断机制只应该处理**成功的流式内容**，不应该处理**错误响应**
2. 错误响应使用 StreamingResponse 会被误判为"需要续写的截断内容"
3. 普通 Response 格式更明确，不会触发抗截断机制
4. 让外层的重试逻辑（刷新 token、切换凭证）能够正常执行

这个修复解决了两个问题：
1. **空响应问题**：不再返回 `200 -`
2. **重试逻辑失效**：404 错误现在会正确刷新 token 并切换凭证
