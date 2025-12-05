# 重试逻辑全面分析（2025-12-05）

## 目录
1. [当前重试逻辑结构](#当前重试逻辑结构)
2. [GeminiCLI 重试逻辑](#geminicli-重试逻辑)
3. [Antigravity 重试逻辑](#antigravity-重试逻辑)
4. [抗截断重试逻辑](#抗截断重试逻辑)
5. [发现的问题](#发现的问题)
6. [优化建议](#优化建议)

---

## 当前重试逻辑结构

### 重试层次（从外到内）

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: 抗截断机制（Anti-truncation）               │
│  - 位置：src/anti_truncation.py                      │
│  - 最多重试：3 次（检测 [done] 标记）                │
│  - 适用范围：启用抗截断的流式请求                    │
└─────────────────────────────────────────────────────┘
                    ↓ 调用
┌─────────────────────────────────────────────────────┐
│ Layer 2: GeminiCLI 重试逻辑                          │
│  - 位置：src/google_chat_api.py:263                  │
│  - 最多重试：5 次（max_retries）                     │
│  - 处理：429, 400, 401, 403, 404, 5xx               │
└─────────────────────────────────────────────────────┘
                    ↓ 或
┌─────────────────────────────────────────────────────┐
│ Layer 2: Antigravity 重试逻辑                        │
│  - 位置：src/openai_router.py:691                    │
│  - 最多重试：5 次                                    │
│  - 处理：429, 400, 401, 403, 404, 5xx               │
└─────────────────────────────────────────────────────┘
```

### 问题：多层重试可能导致过度重试

- 抗截断启用时：每次 API 请求最多可能重试 **3 × 5 = 15 次**
- 用户体验差：长时间等待才返回错误

---

## GeminiCLI 重试逻辑

### 文件：`src/google_chat_api.py`

### 重试循环结构

```python
# 第 263 行
for attempt in range(max_retries + 1):  # max_retries = 5，实际执行 6 次（0-5）
    try:
        if is_streaming:
            # 流式请求处理
            # ... 429 处理 ...
            # ... 非 200 状态码处理 ...
        else:
            # 非流式请求处理
            # ... 429 处理 ...
            # ... 非 200 状态码处理 ...
    except Exception as e:
        # 异常处理
        if attempt < max_retries:
            delay = retry_interval * (2 ** attempt)
            await asyncio.sleep(delay)
            continue
```

### 错误处理分支

#### 1. 流式请求（第 265-448 行）

##### 1.1 429 错误（第 276-331 行）
```python
if resp.status_code == 429:
    # 记录错误
    await credential_manager.record_api_call_result(current_file, False, 429)

    # 清理资源
    await stream_ctx.__aexit__(None, None, None)
    await client.aclose()

    if retry_429_enabled and attempt < max_retries:
        # 指数退避：1s, 2s, 4s, 8s, 16s
        delay = retry_interval * (2 ** attempt)

        # 强制轮换凭证
        await credential_manager.force_rotate_credential()

        # 获取下一个凭证
        next_cred_result = await _get_next_credential(...)
        if next_cred_result:
            current_file, credential_data, headers, final_post_data, target_url = next_cred_result

        await asyncio.sleep(delay)
        continue  # 重试
    else:
        return _create_error_response("429 rate limit exceeded", 429)
```

**问题**：
- ❌ 429 错误后先切换凭证，再延迟重试
- ❌ 即使新凭证可用，也要等待指数退避延迟
- ✅ 正确：清理了资源

##### 1.2 非 200 状态码（第 332-428 行）

###### 5xx 服务器错误（第 374-382 行）
```python
if 500 <= resp.status_code < 600 and attempt < max_retries:
    delay = retry_interval * (2 ** attempt)
    await asyncio.sleep(delay)
    continue  # 使用同一凭证重试
```

**逻辑**：
- ✅ 正确：5xx 不切换凭证（服务器问题）
- ✅ 正确：指数退避

###### 400/401/404 错误（第 384-408 行）
```python
should_auto_ban = await _check_should_auto_ban(resp.status_code)

if should_auto_ban and credential_manager and attempt < max_retries:
    if resp.status_code in (400, 401, 404):
        # 先尝试刷新 token
        refresh_success = await credential_manager.force_refresh_current_token()

        if refresh_success:
            # 使用同一凭证重试
            next_cred_result = await _get_next_credential(...)
            await asyncio.sleep(0.5)
            continue

        # 刷新失败，继续走封禁流程
```

**问题**：
- ⚠️ `_get_next_credential()` 获取的是"当前刷新后的凭证"还是"下一个凭证"？命名容易误导
- ⚠️ 400 错误不应该刷新 token（参数错误），应该直接返回
- ✅ 401/404 刷新 token 是合理的

###### 403 或 token 刷新失败（第 410-427 行）
```python
# 封禁当前凭证并切换
await _handle_auto_ban(credential_manager, resp.status_code, current_file)

next_cred_result = await _get_next_credential(...)
if next_cred_result:
    current_file, credential_data, headers, final_post_data, target_url = next_cred_result

await asyncio.sleep(0.5)
continue
```

**问题**：
- ❌ 没有清理之前的 `stream_ctx` 和 `client` 资源！
- ❌ 可能导致资源泄漏

#### 2. 非流式请求（第 449-583 行）

逻辑类似流式请求，但：
- ✅ 没有资源清理问题（使用 `async with`）
- ⚠️ 同样的 400 错误刷新 token 问题

---

## Antigravity 重试逻辑

### 文件：`src/openai_router.py`

### 重试循环结构

```python
# 第 691 行
for attempt in range(max_retries):  # max_retries = 5，实际执行 5 次（0-4）
    try:
        # 流式请求
        async for chunk in stream_generate_content(...):
            # 转换并发送

        # 成功返回
        await ant_cred_mgr.mark_credential_success(virtual_filename)
        return

    except Exception as e:
        error_code = _extract_error_code_from_exception(error_message)

        # 5xx 服务器错误
        if error_code and 500 <= error_code < 600 and attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            await asyncio.sleep(delay)
            continue  # 使用同一凭证重试

        # 检查是否需要重试
        should_retry = await _check_should_retry_antigravity(error_code, auto_ban_error_codes)

        if should_retry and attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            await asyncio.sleep(delay)

            # 切换到下一个凭证
            credential_result = await ant_cred_mgr.get_valid_credential()
            if credential_result:
                virtual_filename, account = credential_result
                access_token = account.get("access_token")

            continue
```

**问题**：
- ✅ 逻辑清晰，5xx 不切换凭证
- ✅ 其他错误切换凭证
- ⚠️ `max_retries - 1` 导致实际只重试 4 次（0-3），而不是 5 次
- ❌ 没有 400/401/404 刷新 token 的逻辑

---

## 抗截断重试逻辑

### 文件：`src/anti_truncation.py`

### 重试循环结构

```python
# 第 202 行
while self.current_attempt < self.max_attempts:  # max_attempts = 3
    self.current_attempt += 1

    # 构建续写请求
    current_payload = self._build_current_payload()

    # 发送请求
    response = await self.original_request_func(current_payload)

    # 检查是否有 [done] 标记
    if found_done_marker:
        return

    # 没有 [done] 标记，继续重试
    if self.current_attempt < self.max_attempts:
        continue
    else:
        # 最后一次尝试，结束
        return
```

**问题**：
- ❌ **不区分错误类型**：即使是 400/401/404 错误，也会重试
- ❌ **误判错误响应为截断**：刚才修复的问题
- ❌ **与外层重试逻辑冲突**：抗截断重试 3 次后，外层可能再重试 5 次
- ⚠️ 每次重试都调用 `original_request_func`，会触发外层的整个重试逻辑

---

## 发现的问题

### 🔴 严重问题

#### 1. 流式请求资源泄漏
**位置**：`src/google_chat_api.py:410-427`

```python
# 封禁当前凭证并切换
await _handle_auto_ban(credential_manager, resp.status_code, current_file)

# ❌ 问题：没有清理 stream_ctx 和 client
next_cred_result = await _get_next_credential(...)
await asyncio.sleep(0.5)
continue  # 重试，但之前的资源没清理
```

**影响**：
- 每次切换凭证都泄漏一个 HTTP 连接
- 多次重试后可能耗尽连接池

#### 2. 抗截断误判错误响应
**位置**：`src/anti_truncation.py:214-217`

```python
response = await self.original_request_func(current_payload)

if not isinstance(response, StreamingResponse):
    # 非流式响应，直接处理
    yield await self._handle_non_streaming_response(response)
    return
```

**问题**：
- 只检查是否为 `StreamingResponse`
- 不检查响应状态码（可能是 400/401/404 错误）
- **已修复**：错误响应改为返回普通 `Response`

#### 3. 400 错误不应该刷新 token
**位置**：`src/google_chat_api.py:388-408`

```python
if resp.status_code in (400, 401, 404):
    refresh_success = await credential_manager.force_refresh_current_token()
```

**问题**：
- 400 是参数错误（如 `thinking_budget` 超出范围）
- 刷新 token 不会解决参数错误
- 应该直接返回错误给用户

### ⚠️ 中等问题

#### 4. 重试次数不一致
- GeminiCLI：`range(max_retries + 1)` → 实际 6 次（0-5）
- Antigravity：`range(max_retries)` → 实际 5 次（0-4）
- 抗截断：固定 3 次

#### 5. 命名容易误导
- `_get_next_credential()` 在刷新 token 后获取"当前凭证"
- 但命名暗示"下一个凭证"

#### 6. 429 错误延迟不合理
```python
# 切换凭证后还要等待
await credential_manager.force_rotate_credential()
await asyncio.sleep(delay)  # ❌ 新凭证可用，不需要延迟
```

### 🟡 小问题

#### 7. 代码重复
- 流式和非流式的重试逻辑几乎完全相同
- 可以提取公共函数

---

## 优化建议

### 方案 1：简化重试层次（推荐）

#### 移除抗截断的重试逻辑
- 抗截断只负责**检测**和**续写**
- 不负责**错误处理**和**重试**

```python
# 修改 anti_truncation.py
async def handle_streaming_with_continuation(self):
    # 只执行一次请求
    response = await self.original_request_func(self.original_payload)

    # 如果是错误响应，直接返回
    if not isinstance(response, StreamingResponse):
        yield await self._handle_non_streaming_response(response)
        return

    # 检查是否截断，如果截断则续写
    for continuation_attempt in range(self.max_continuation_attempts):
        # ... 检测 [done] 标记 ...
        if found_done_marker:
            return

        # 没有 [done]，发送续写请求
        continuation_payload = self._build_continuation_payload()
        response = await self.original_request_func(continuation_payload)
        # ... 继续处理 ...
```

#### 统一重试逻辑

```python
# 新增 src/retry_handler.py
class RetryHandler:
    def __init__(self, max_retries=5, base_delay=1.0):
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def execute_with_retry(self, request_func, credential_manager):
        for attempt in range(self.max_retries):
            try:
                response = await request_func()

                # 检查状态码
                if response.status_code == 200:
                    return response

                # 错误处理
                should_retry = await self._handle_error(
                    response, attempt, credential_manager
                )

                if not should_retry:
                    return response

            except Exception as e:
                # 异常处理
                pass

        # 最大重试次数
        return error_response

    async def _handle_error(self, response, attempt, credential_manager):
        status_code = response.status_code

        # 5xx: 不切换凭证，指数退避
        if 500 <= status_code < 600:
            if attempt < self.max_retries - 1:
                await self._exponential_backoff(attempt)
                return True
            return False

        # 429: 切换凭证，无延迟
        if status_code == 429:
            if attempt < self.max_retries - 1:
                await credential_manager.force_rotate_credential()
                return True
            return False

        # 401/404: 先刷新 token，失败则切换凭证
        if status_code in (401, 404):
            if attempt < self.max_retries - 1:
                refresh_success = await credential_manager.force_refresh_current_token()
                if not refresh_success:
                    await credential_manager.force_rotate_credential()
                await asyncio.sleep(0.5)
                return True
            return False

        # 403: 切换凭证
        if status_code == 403:
            if attempt < self.max_retries - 1:
                await credential_manager.force_rotate_credential()
                await asyncio.sleep(0.5)
                return True
            return False

        # 400: 参数错误，不重试
        if status_code == 400:
            return False

        return False

    async def _exponential_backoff(self, attempt):
        delay = self.base_delay * (2 ** attempt)
        await asyncio.sleep(delay)
```

### 方案 2：修复当前逻辑（快速修复）

#### 修复 1：400 错误不刷新 token
```python
# src/google_chat_api.py
if resp.status_code in (401, 404):  # 移除 400
    refresh_success = await credential_manager.force_refresh_current_token()
    # ...

# 400 错误直接返回
if resp.status_code == 400:
    return _create_error_response(f"Invalid request: {response_content}", 400)
```

#### 修复 2：清理流式请求资源
```python
# src/google_chat_api.py:410-427
# 封禁当前凭证并切换
await _handle_auto_ban(credential_manager, resp.status_code, current_file)

# 清理资源
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

#### 修复 3：429 切换凭证后不延迟
```python
# src/google_chat_api.py
if retry_429_enabled and attempt < max_retries:
    # 切换凭证
    await credential_manager.force_rotate_credential()
    next_cred_result = await _get_next_credential(...)

    # ❌ 移除：await asyncio.sleep(delay)
    # ✅ 新凭证可用，立即重试
    continue
```

#### 修复 4：统一重试次数
```python
# src/openai_router.py
for attempt in range(max_retries):  # 改为 range(max_retries + 1)
    # ...
    if attempt < max_retries:  # 改为 attempt < max_retries
        continue
```

---

## 总结

### 当前状态
- ❌ 重试逻辑分散在 3 个地方
- ❌ 多层重试可能导致过度重试（最多 15 次）
- ❌ 存在资源泄漏风险
- ❌ 400 错误处理不正确

### 推荐行动
1. **立即修复**：方案 2 的 4 个修复（修复资源泄漏和 400 错误）
2. **中期优化**：考虑方案 1（重构重试逻辑）
3. **监控**：添加重试次数统计，监控是否过度重试
