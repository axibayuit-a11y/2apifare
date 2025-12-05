# 修复 404 错误和空回响问题

## 问题描述

从日志 `docs/2apifare-20251204101333.log` 中发现：

1. **大量 404 错误**：Gemini CLI 请求返回 404 "Requested entity was not found"
2. **空回响问题**：服务器返回 `200 -`（状态码 200 但无内容）
3. **重试机制失效**：404 错误后没有触发凭证轮换和重试

## 根本原因

### 问题 1: 404 错误不触发重试

**原因**：404 不在 `AUTO_BAN_ERROR_CODES` 列表中（默认只有 401, 403）

```python
# config.py (修复前)
AUTO_BAN_ERROR_CODES = [401, 403]
```

当 Gemini API 返回 404 时：
- `_check_should_auto_ban(404)` 返回 `False`
- 不触发凭证轮换
- 直接返回错误给客户端
- 客户端收到空响应

### 问题 2: Antigravity 空回响

**原因**：thinking 内容被过滤但空字符串仍被发送

```python
# antigravity/client.py
if chunk_type == 'thinking':
    return ""  # 返回空字符串

# src/openai_router.py (修复前)
openai_chunk = convert_sse_to_openai_format(chunk, ...)
yield openai_chunk.encode()  # 空字符串也被发送
```

## 解决方案

### 修复 1: 404 错误先刷新 token，失败后再切换账号

**步骤 1：将 404 添加到自动封禁列表**
```python
# config.py
AUTO_BAN_ERROR_CODES = [401, 403, 404]
```

**步骤 2：404 错误先尝试刷新 token**
```python
# src/google_chat_api.py
if resp.status_code in (400, 401, 404):
    # 先尝试刷新 token
    refresh_success = await credential_manager.force_refresh_current_token()
    if refresh_success:
        # Token 刷新成功，使用同一凭证重试
        continue
    # Token 刷新失败，继续走封禁流程
```

**效果**：
1. 404 错误触发 `_check_should_auto_ban(404)` 返回 `True`
2. **先尝试刷新当前账号的 token**（可能是 token 过期导致的 404）
3. 如果刷新成功，使用同一账号重试
4. 如果刷新失败，禁用当前凭证并切换到下一个账号
5. 重试最多 5 次（`max_retries = 5`）

### 修复 2: 过滤空的 thinking 响应

```python
# src/openai_router.py
openai_chunk = convert_sse_to_openai_format(chunk, base_model, stream_id, created)

# 只发送非空内容（过滤掉 thinking 返回的空字符串）
if openai_chunk:
    yield openai_chunk.encode()
```

**效果**：
- thinking 内容被完全过滤，不发送给客户端
- 避免空响应导致的客户端错误

## 404 错误的常见原因

1. **模型不存在**：请求的模型名称错误或已下线
2. **账号无权限**：当前账号没有访问该模型的权限
3. **端点错误**：使用了错误的 API 端点
4. **配额耗尽**：某些情况下 Google API 返回 404 而不是 429

## 重试流程

```
请求 -> 404 错误
  ↓
检查 should_auto_ban(404) -> True
  ↓
尝试刷新当前账号的 token
  ↓
  ├─ 刷新成功 -> 使用同一账号重试
  │
  └─ 刷新失败 -> 禁用当前凭证
                  ↓
                轮换到下一个凭证
                  ↓
                延迟 0.5 秒
                  ↓
                重试（最多 5 次）
                  ↓
                如果所有凭证都失败 -> 返回错误
```

## 测试建议

1. **测试 404 重试**：
   - 使用一个无权限的账号
   - 请求一个不存在的模型
   - 观察是否自动切换到其他账号

2. **测试空回响修复**：
   - 使用 Antigravity 的 thinking 模型（如 claude-sonnet-4-5-thinking）
   - 确认不会收到空响应
   - 确认 thinking 内容被正确过滤

## 相关文件

- `config.py`: 添加 404 到 AUTO_BAN_ERROR_CODES
- `src/google_chat_api.py`: 404 错误处理逻辑
- `src/openai_router.py`: 过滤空的 thinking 响应
- `antigravity/client.py`: thinking 内容转换逻辑

## 日志示例

### 修复前
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[INFO] 172.18.0.1:37650 - - POST /v1/chat/completions 1.1" 200 - "-" "Go-http-client/2.0"
```

### 修复后（刷新成功）
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[AUTH REFRESH] 404 error, attempting token refresh before retry (1/5)
[INFO] Token刷新成功并已保存: gemini-key-xxx.json
[AUTH REFRESH] Token refreshed, retrying with same credential
[INFO] Request succeeded with same credential
```

### 修复后（刷新失败，切换账号）
```
[ERROR] Google API returned status 404 (STREAMING). Response details: {...}
[AUTH REFRESH] 404 error, attempting token refresh before retry (1/5)
[WARNING] [AUTH REFRESH] Token refresh failed, proceeding with credential ban
[RETRY] 404 error encountered, rotating credential and retrying (1/5)
[INFO] Rotated to credential index 1
[INFO] Using credential: another-account@gmail.com
```
