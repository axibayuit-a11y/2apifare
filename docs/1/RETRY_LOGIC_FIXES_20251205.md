# 重试逻辑修复总结（2025-12-05）

## 修复概览

本次修复解决了重试逻辑中的多个严重问题，提升了系统的稳定性和用户体验。

### 修复的问题

| 问题 | 严重程度 | 状态 |
|------|----------|------|
| 1. thinking_budget 超出范围 | 🔴 严重 | ✅ 已修复 |
| 2. 400 错误误刷新 token | 🔴 严重 | ✅ 已修复 |
| 3. 流式请求资源泄漏 | 🔴 严重 | ✅ 已修复 |
| 4. 抗截断误判错误响应 | 🔴 严重 | ✅ 已修复 |
| 5. 429 切换凭证后不必要的延迟 | ⚠️ 中等 | ✅ 已修复 |
| 6. 重试次数不一致 | 🟡 小 | ⏸️ 暂不修复 |

---

## 修复 1：thinking_budget 超出范围

### 问题
```
thinking_budget is out of range; supported values are integers from 512 to 24576
```

Google API 要求：
- **最小值**：512
- **最大值**：24576

代码中的错误值：
- `nothinking` 模式：128（低于最小值）
- `maxthinking` Pro 模式：32768（超过最大值）

### 修复代码

**文件**：[config.py:71-86](d:/Research/fandai/2apifare/config.py#L71-L86)

```python
def get_thinking_budget(model_name):
    """
    Get the appropriate thinking budget for a model based on its name and variant.

    Google API支持的范围：512 - 24576
    参考：https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/gemini
    """

    if is_nothinking_model(model_name):
        return 512  # 最小值（之前是 128，低于 API 最小值）
    elif is_maxthinking_model(model_name):
        # 所有 maxthinking 模型都使用最大值
        return 24576  # API 支持的最大值（之前 Pro 模型设置为 32768 超出范围）
    else:
        # Default thinking budget for regular models
        return None  # Default for all models
```

### 修复效果

- ✅ `-nothinking` 模型：128 → 512
- ✅ `-maxthinking` 模型：32768 → 24576（Pro）/ 24576（Flash）
- ✅ 不再出现 `400 INVALID_ARGUMENT` 错误

---

## 修复 2：400 错误误刷新 token

### 问题

代码将 **400 参数错误**与 **401/404 认证错误**混为一谈：

```python
# 修复前
if resp.status_code in (400, 401, 404):  # ❌ 400 是参数错误，不应该刷新 token
    refresh_success = await credential_manager.force_refresh_current_token()
```

**问题**：
- 400 错误是**请求参数错误**（如 thinking_budget 超出范围）
- 刷新 token 无法解决参数错误
- 导致无意义的重试，浪费时间

### 修复代码

**文件**：[google_chat_api.py:375-392](d:/Research/fandai/2apifare/src/google_chat_api.py#L375-L392)（流式）、[google_chat_api.py:535-542](d:/Research/fandai/2apifare/src/google_chat_api.py#L535-L542)（非流式）

```python
# [FIX] 400 错误是参数错误，直接返回，不刷新 token
if resp.status_code == 400:
    log.error(
        f"[BAD REQUEST] Invalid request parameters. Response: {response_content[:500]}"
    )
    # 清理资源
    try:
        await stream_ctx.__aexit__(None, None, None)
    except:
        pass
    try:
        await client.aclose()
    except:
        pass

    return _create_error_response(
        f"Invalid request: {response_content}", 400
    )

# 401/404 错误：先尝试刷新 token（可能是 token 过期）
if resp.status_code in (401, 404):  # ✅ 移除了 400
    refresh_success = await credential_manager.force_refresh_current_token()
    # ...
```

### 修复效果

- ✅ 400 错误直接返回，不浪费时间重试
- ✅ 401/404 错误仍然刷新 token（这是正确的）
- ✅ 用户能立即看到参数错误信息

---

## 修复 3：流式请求资源泄漏

### 问题

切换凭证时，**没有清理之前的 HTTP 连接资源**：

```python
# 修复前
await _handle_auto_ban(credential_manager, resp.status_code, current_file)

# ❌ 问题：没有清理 stream_ctx 和 client
next_cred_result = await _get_next_credential(...)
await asyncio.sleep(0.5)
continue  # 重试，但之前的资源没清理
```

**影响**：
- 每次切换凭证都泄漏一个 HTTP 连接
- 多次重试后可能耗尽连接池
- 长时间运行导致内存泄漏

### 修复代码

**文件**：[google_chat_api.py:437-445](d:/Research/fandai/2apifare/src/google_chat_api.py#L437-L445)（403切换凭证）、[google_chat_api.py:408-416](d:/Research/fandai/2apifare/src/google_chat_api.py#L408-L416)（401/404刷新成功）

```python
# 403 或 token 刷新失败：封禁当前凭证并切换到下一个凭证重试
await _handle_auto_ban(credential_manager, resp.status_code, current_file)

# [FIX] 清理当前资源（防止资源泄漏）
try:
    await stream_ctx.__aexit__(None, None, None)
except:
    pass
try:
    await client.aclose()
except:
    pass

# 获取下一个凭证
next_cred_result = await _get_next_credential(...)
```

### 修复效果

- ✅ 每次切换凭证前都清理资源
- ✅ 防止 HTTP 连接池耗尽
- ✅ 避免长时间运行的内存泄漏

---

## 修复 4：抗截断误判错误响应

### 问题

错误响应使用 `StreamingResponse` 格式：

```python
# 修复前
async def error_stream():
    error_response = {"error": {...}}
    yield f"data: {json.dumps(error_response)}\n\n"

return StreamingResponse(
    error_stream(),
    media_type="text/event-stream",
    status_code=resp.status_code  # ❌ 会被抗截断误判
)
```

**问题流程**：
```
404 错误 → StreamingResponse(错误)
    ↓
抗截断检测到流式响应
    ↓
检测到没有 [done] 标记
    ↓
误判为"截断"，重试 3 次
    ↓
最终返回空响应（200 -）
```

### 修复代码

**文件**：[google_chat_api.py:644-657](d:/Research/fandai/2apifare/src/google_chat_api.py#L644-L657)、[google_chat_api.py:327-331](d:/Research/fandai/2apifare/src/google_chat_api.py#L327-L331)、[google_chat_api.py:424-428](d:/Research/fandai/2apifare/src/google_chat_api.py#L424-L428)

```python
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
    media_type="application/json",  # ✅ 普通 JSON 响应
)
```

### 修复效果

- ✅ 错误响应不触发抗截断机制
- ✅ 外层重试逻辑（刷新 token、切换凭证）能正常执行
- ✅ 不再出现空响应（200 -）

---

## 修复 5：429 切换凭证后不必要的延迟

### 问题

429 错误切换到新凭证后，仍然等待指数退避延迟：

```python
# 修复前
await credential_manager.force_rotate_credential()
next_cred_result = await _get_next_credential(...)

await asyncio.sleep(delay)  # ❌ 新凭证可用，不需要延迟
continue
```

**问题**：
- 429 是配额限制，新凭证可能有配额
- 不必要的延迟降低用户体验
- 如果有 10 个凭证，切换一次就要等待 1-16 秒

### 修复代码

**文件**：[google_chat_api.py:308-337](d:/Research/fandai/2apifare/src/google_chat_api.py#L308-L337)（流式）、[google_chat_api.py:511-540](d:/Research/fandai/2apifare/src/google_chat_api.py#L511-L540)（非流式）

```python
# 如果重试可用且未达到最大次数，进行重试
if retry_429_enabled and attempt < max_retries:
    if credential_manager:
        # 429错误时强制轮换凭证，不增加调用计数
        await credential_manager.force_rotate_credential()
        # 获取下一个凭证
        next_cred_result = await _get_next_credential(...)

        if next_cred_result:
            current_file, credential_data, headers, final_post_data, target_url = next_cred_result
            # [FIX] 成功切换到新凭证，立即重试（新凭证可能有配额）
            log.info(f"[RETRY] 429 error, switched to new credential, retrying immediately ({attempt + 1}/{max_retries})")
            continue
        else:
            # 没有其他可用凭证，指数退避
            delay = retry_interval * (2 ** attempt)
            log.warning(
                f"[RETRY] 429 error, no other credentials available, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)
            continue
```

### 修复效果

- ✅ 成功切换到新凭证时：立即重试（0 延迟）
- ✅ 没有其他可用凭证时：指数退避（合理延迟）
- ✅ 提升用户体验，减少等待时间

---

## 修复前后对比

### 400 错误处理

#### 修复前
```
400 错误 → 刷新 token（失败）→ 切换凭证 → 重试
    ↓
再次 400 → 刷新 token（失败）→ 切换凭证 → 重试
    ↓
... 重复 5 次 ...
    ↓
最终返回 400 错误（浪费了大量时间）
```

#### 修复后
```
400 错误 → 立即返回错误给用户
    ↓
用户能立即看到问题并修复参数
```

### 429 错误处理

#### 修复前
```
429 错误 → 切换凭证 → 等待 1 秒 → 重试
    ↓
429 错误 → 切换凭证 → 等待 2 秒 → 重试
    ↓
429 错误 → 切换凭证 → 等待 4 秒 → 重试
    ↓
总延迟：7 秒（即使有可用凭证）
```

#### 修复后
```
429 错误 → 切换凭证 → 立即重试 → 成功
    ↓
总延迟：<1 秒
```

### 404 错误处理

#### 修复前
```
404 错误 → StreamingResponse(错误)
    ↓
抗截断误判 → 重试 3 次（都是 404）
    ↓
返回空响应（200 -）← 用户困惑
```

#### 修复后
```
404 错误 → 尝试刷新 token
    ↓
刷新成功 → 使用同一凭证重试 → 成功
刷新失败 → 切换凭证 → 重试 → 成功
    ↓
如果都失败 → 返回明确的 404 错误
```

---

## 测试建议

### 1. 测试 thinking_budget 修复

```bash
# 测试 nothinking 模型
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-pro-nothinking",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 预期：不再出现 400 错误
```

### 2. 测试 400 错误直接返回

```bash
# 故意使用错误参数
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 999  // 无效值
  }'

# 预期：
# 1. 立即返回 400 错误（不重试）
# 2. 日志显示：[BAD REQUEST] Invalid request parameters
```

### 3. 测试 429 智能延迟

**前提**：准备多个凭证

```bash
# 触发 429 错误（快速发送多个请求）
for i in {1..100}; do
  curl -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "Hello"}]}'
done

# 预期日志：
# [RETRY] 429 error, switched to new credential, retrying immediately
# 而不是：
# [RETRY] 429 error encountered, waiting 1.0s before retry
```

### 4. 测试 404 错误重试

```bash
# 使用无权限的凭证
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 预期日志：
# [AUTH REFRESH] 404 error, attempting token refresh before retry
# [AUTH REFRESH] Token refreshed, retrying with same credential
# 或
# [AUTH REFRESH] Token refresh failed, proceeding with credential ban
# [RETRY] 404 error encountered, rotating credential and retrying
```

### 5. 测试资源清理（长时间运行）

```bash
# 运行 1 小时，监控连接数
watch -n 60 'netstat -an | grep ESTABLISHED | wc -l'

# 预期：连接数稳定，不持续增长
```

---

## 相关文档

- [RETRY_LOGIC_ANALYSIS.md](./RETRY_LOGIC_ANALYSIS.md) - 重试逻辑全面分析
- [FIX_EMPTY_RESPONSE_20251205.md](./FIX_EMPTY_RESPONSE_20251205.md) - 空响应问题修复
- [FIX_404_EMPTY_RESPONSE.md](./FIX_404_EMPTY_RESPONSE.md) - 404 错误修复（旧版）

---

## 未来优化建议

### 1. 重构重试逻辑（长期）

当前重试逻辑分散在多个地方，建议：
- 创建统一的 `RetryHandler` 类
- 提取公共重试逻辑
- 减少代码重复

### 2. 添加重试指标监控

建议添加以下指标：
- 每种错误码的重试次数
- 重试成功率
- 平均重试延迟
- 凭证切换次数

### 3. 优化抗截断机制

当前抗截断机制会增加重试次数（最多 3×5=15 次），建议：
- 抗截断只负责检测和续写
- 不负责错误重试
- 与外层重试逻辑分离

---

## 总结

本次修复解决了 5 个关键问题：

| 修复项 | 影响 | 效果 |
|--------|------|------|
| thinking_budget 范围 | 🔴 高 | 消除 400 错误循环 |
| 400 错误误刷新 | 🔴 高 | 节省重试时间 |
| 资源泄漏 | 🔴 高 | 防止连接池耗尽 |
| 抗截断误判 | 🔴 高 | 消除空响应问题 |
| 429 智能延迟 | ⚠️ 中 | 提升用户体验 |

**预期收益**：
- ✅ 更快的错误响应
- ✅ 更少的无效重试
- ✅ 更好的资源管理
- ✅ 更高的系统稳定性
