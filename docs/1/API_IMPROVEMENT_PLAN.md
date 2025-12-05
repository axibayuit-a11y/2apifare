# API 改进计划 - 降低 403/429 错误率

> 基于 AIClient-2-API 项目代码分析，总结可借鉴的优化策略

## 📊 对比分析总结

| 功能 | AIClient-2-API | 我们的实现 | 优先级 |
|------|---------------|-----------|--------|
| 多端点降级 | ✅ 支持 | ✅ 已实现 | ✅ 完成 |
| 指数退避重试 | ✅ 支持 | ✅ 已实现 | ✅ 完成 |
| 401/400 自动刷新重试 | ✅ 支持 | ✅ 已实现 | ✅ 完成 |
| 5xx 服务器错误重试 | ✅ 支持 | ✅ 已实现 | ✅ 完成 |
| 网络错误降级 | ✅ 支持 | ✅ 已实现 | ✅ 完成 |
| Token 提前刷新 | ✅ 50分钟 | ✅ 5分钟 | ✅ 已实现 |
| 删除 safetySettings | ✅ 删除 | ✅ BLOCK_NONE (正确) | ⏭️ 不适用 |
| 删除 maxOutputTokens | ✅ 删除 | ✅ 已删除 | ✅ 完成 |

---

## 🔧 改进项 1：多端点降级机制

### 问题描述
当前只使用单一 API 端点，当该端点出现问题时无法自动切换。

### AIClient-2-API 实现方式
```javascript
// 多环境降级顺序
this.baseURLs = [
    'https://daily-cloudcode-pa.sandbox.googleapis.com',
    'https://autopush-cloudcode-pa.sandbox.googleapis.com'
];

async callApi(method, body, isRetry = false, retryCount = 0, baseURLIndex = 0) {
    if (baseURLIndex >= this.baseURLs.length) {
        throw new Error('All Antigravity base URLs failed');
    }
    
    const baseURL = this.baseURLs[baseURLIndex];
    
    try {
        // 发送请求...
    } catch (error) {
        // 429 时切换端点
        if (error.response?.status === 429) {
            if (baseURLIndex + 1 < this.baseURLs.length) {
                console.log(`Rate limited on ${baseURL}. Trying next base URL...`);
                return this.callApi(method, body, isRetry, retryCount, baseURLIndex + 1);
            }
        }
        
        // 网络错误时切换端点
        if (!error.response && baseURLIndex + 1 < this.baseURLs.length) {
            console.log(`Network error on ${baseURL}. Trying next base URL...`);
            return this.callApi(method, body, isRetry, retryCount, baseURLIndex + 1);
        }
    }
}
```

### 我们需要修改的文件
- `config.py` - 添加多端点配置
- `antigravity/client.py` - 实现端点切换逻辑
- `src/google_chat_api.py` - CLI 反代也需要支持

### 实现建议
```python
# config.py
ANTIGRAVITY_API_ENDPOINTS = [
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent",
    "https://autopush-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent"
]

CLI_API_ENDPOINTS = [
    "https://cloudcode-pa.googleapis.com/v1internal",
    # 可添加备用端点
]
```

---

## 🔧 改进项 2：指数退避重试

### 问题描述
当前使用固定重试间隔（`retry_429_interval`），没有指数退避。

### AIClient-2-API 实现方式
```javascript
const maxRetries = this.config.REQUEST_MAX_RETRIES || 3;
const baseDelay = this.config.REQUEST_BASE_DELAY || 1000; // 1秒基础延迟

// 指数退避：1s, 2s, 4s, 8s...
const delay = baseDelay * Math.pow(2, retryCount);
console.log(`Retrying in ${delay}ms... (attempt ${retryCount + 1}/${maxRetries})`);
await new Promise(resolve => setTimeout(resolve, delay));
```

### 我们需要修改的文件
- `config.py` - 添加 `RETRY_BASE_DELAY` 配置
- `src/google_chat_api.py` - 修改重试逻辑

### 实现建议
```python
# 当前实现
await asyncio.sleep(retry_interval)  # 固定间隔

# 改进后
base_delay = await get_retry_base_delay()  # 默认 1.0 秒
delay = base_delay * (2 ** attempt)  # 指数退避
log.info(f"[RETRY] Waiting {delay:.1f}s before retry (attempt {attempt + 1}/{max_retries})")
await asyncio.sleep(delay)
```

---

## 🔧 改进项 3：401/400 自动刷新 Token 并重试

### 问题描述
当前遇到 401/400 错误直接失败，没有尝试刷新 token 后重试。

### AIClient-2-API 实现方式
```javascript
async callApi(method, body, isRetry = false, retryCount = 0) {
    try {
        // 发送请求...
    } catch (error) {
        // 401/400 时刷新 token 并重试（只重试一次）
        if ((error.response?.status === 400 || error.response?.status === 401) && !isRetry) {
            console.log('Received 401/400. Refreshing auth and retrying...');
            await this.initializeAuth(true);  // 强制刷新 token
            return this.callApi(method, body, true, retryCount);  // 标记为重试
        }
        throw error;
    }
}
```

### 我们需要修改的文件
- `src/google_chat_api.py` - 添加 401/400 处理逻辑
- `src/antigravity_credential_manager.py` - 添加强制刷新方法

### 实现建议
```python
async def send_gemini_request(..., is_auth_retry=False):
    try:
        # 发送请求...
    except Exception as e:
        status_code = extract_status_code(e)
        
        # 401/400 时刷新 token 并重试（只重试一次）
        if status_code in (400, 401) and not is_auth_retry:
            log.warning("[AUTH] Received 401/400. Refreshing token and retrying...")
            
            # 强制刷新当前凭证的 token
            await credential_manager.force_refresh_current_token()
            
            # 重新获取凭证并重试
            return await send_gemini_request(..., is_auth_retry=True)
        
        raise
```

---

## 🔧 改进项 4：5xx 服务器错误重试

### 问题描述
当前只处理 429 错误的重试，没有处理 5xx 服务器错误。

### AIClient-2-API 实现方式
```javascript
// 5xx 服务器错误重试
if (error.response?.status >= 500 && error.response?.status < 600 && retryCount < maxRetries) {
    const delay = baseDelay * Math.pow(2, retryCount);
    console.log(`Server error ${error.response.status}. Retrying in ${delay}ms...`);
    await new Promise(resolve => setTimeout(resolve, delay));
    return this.callApi(method, body, isRetry, retryCount + 1, baseURLIndex);
}
```

### 我们需要修改的文件
- `src/google_chat_api.py` - 添加 5xx 处理逻辑
- `config.py` - 可选：添加 `RETRY_5XX_ENABLED` 配置

### 实现建议
```python
if 500 <= status_code < 600 and attempt < max_retries:
    delay = base_delay * (2 ** attempt)
    log.warning(f"[RETRY] Server error {status_code}. Retrying in {delay:.1f}s...")
    await asyncio.sleep(delay)
    continue  # 继续重试
```

---

## 🔧 改进项 5：网络错误降级

### 问题描述
网络错误（超时、连接失败等）时没有尝试切换端点。

### AIClient-2-API 实现方式
```javascript
// 网络错误（无 response）时切换端点
if (!error.response && baseURLIndex + 1 < this.baseURLs.length) {
    console.log(`Network error on ${baseURL}. Trying next base URL...`);
    return this.callApi(method, body, isRetry, retryCount, baseURLIndex + 1);
}
```

### 实现建议
结合改进项 1（多端点降级）一起实现。

---

## 🔧 改进项 6：删除 safetySettings（可选）

### 问题描述
我们当前设置 `safetySettings` 为 `BLOCK_NONE`，AIClient-2-API 直接删除该字段。

### AIClient-2-API 实现方式
```javascript
// 删除安全设置
if (template.request.safetySettings) {
    delete template.request.safetySettings;
}
```

### 我们当前的实现
```python
# config.py
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    # ...
]
```

### 分析
- 删除 `safetySettings`：让 API 使用默认设置，可能更"正常"
- 设为 `BLOCK_NONE`：明确禁用过滤，但可能被检测为异常行为

### 建议
可以添加配置开关，让用户选择：
```python
# config.py
SAFETY_SETTINGS_MODE = "remove"  # "remove" | "block_none" | "default"
```

---

## 🔧 改进项 7：删除 maxOutputTokens（可选）

### 问题描述
我们保留 `maxOutputTokens`，AIClient-2-API 删除该字段。

### AIClient-2-API 实现方式
```javascript
// 删除 maxOutputTokens
if (template.request.generationConfig && template.request.generationConfig.maxOutputTokens) {
    delete template.request.generationConfig.maxOutputTokens;
}
```

### 分析
- 删除：让 API 使用默认值，可能获得更长的输出
- 保留：可以控制输出长度，但可能触发截断

### 建议
可以添加配置开关：
```python
# config.py
REMOVE_MAX_OUTPUT_TOKENS = True  # 是否删除 maxOutputTokens
```

---

## 📋 实施顺序建议

1. **第一阶段（高优先级）**
   - [x] 改进项 2：指数退避重试 ✅ (2025-12-04 完成)
   - [x] 改进项 3：401/400 自动刷新重试 ✅ (2025-12-04 完成)

2. **第二阶段（中优先级）**
   - [x] 改进项 1：多端点降级机制 ✅ (2025-12-04 完成 - Antigravity)
   - [x] 改进项 4：5xx 服务器错误重试 ✅ (2025-12-04 完成)
   - [x] 改进项 5：网络错误降级 ✅ (2025-12-04 完成 - 包含在改进项1中)

3. **第三阶段（低优先级/可选）**
   - [x] 改进项 6：safetySettings 处理方式 ⏭️ (不适用 - 用户场景需要禁用安全过滤)
   - [x] 改进项 7：maxOutputTokens 处理方式 ✅ (2025-12-04 完成 - 删除该字段)

---

## 📝 参考代码位置

### AIClient-2-API
- `src/gemini/antigravity-core.js` - Antigravity 反代核心
- `src/gemini/gemini-core.js` - CLI 反代核心

### 我们的项目
- `antigravity/client.py` - Antigravity 客户端
- `antigravity/converter.py` - 格式转换器
- `src/google_chat_api.py` - CLI 反代请求处理
- `src/antigravity_credential_manager.py` - 凭证管理
- `config.py` - 配置文件

---

*文档创建时间：2025-12-04*
*基于 AIClient-2-API 项目分析*
