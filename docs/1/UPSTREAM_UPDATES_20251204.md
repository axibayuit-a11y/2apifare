# 上游项目更新分析 (2024-12-04)

本文档记录了原项目 (gcli2api) 的最新更新，以及我们魔改版可以借鉴的改进点。

---

## 📊 项目对比概览

| 项目 | 仓库 | 说明 |
|------|------|------|
| **原项目** | `su-kaka/gcli2api` | 上游原始项目 |
| **魔改版** | 当前项目 | 基于原项目的增强版本 |

---

## 🆕 原项目主要更新

### 1. 分布式存储支持增强

**原项目新增 Postgres 支持**

存储后端优先级：**Redis > Postgres > MongoDB > 本地文件**

```bash
# Postgres 配置示例
export POSTGRES_DSN="postgresql://user:password@localhost:5432/gcli2api"
```

**我们的现状：**
- ✅ 已支持：Redis、MongoDB、本地文件
- ❌ 缺失：Postgres 支持

**建议：** 如果有用户需要关系型数据库，可以考虑添加 Postgres 支持。

---

### 2. 模型列表差异

#### 原项目基础模型
```python
BASE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
]
```

#### 我们的基础模型
```python
BASE_MODELS = [
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",  # 我们独有
]
```

**差异：**
- ✅ 我们多了 `gemini-2.5-flash-lite`
- ✅ 我们有完整的 **Antigravity 模型支持**（原项目没有）

---

### 3. 模型后缀组合支持

**原项目支持多后缀组合：**

```python
# 原项目的实现逻辑
thinking_suffixes = ["-maxthinking", "-nothinking"]
search_suffix = "-search"

# 1. 单独的 thinking 后缀
for thinking_suffix in thinking_suffixes:
    models.append(f"{base_model}{thinking_suffix}")
    models.append(f"假流式/{base_model}{thinking_suffix}")
    models.append(f"流式抗截断/{base_model}{thinking_suffix}")

# 2. 单独的 search 后缀
models.append(f"{base_model}{search_suffix}")
models.append(f"假流式/{base_model}{search_suffix}")
models.append(f"流式抗截断/{base_model}{search_suffix}")

# 3. thinking + search 组合后缀
for thinking_suffix in thinking_suffixes:
    combined_suffix = f"{thinking_suffix}{search_suffix}"
    models.append(f"{base_model}{combined_suffix}")
    models.append(f"假流式/{base_model}{combined_suffix}")
    models.append(f"流式抗截断/{base_model}{combined_suffix}")
```

**支持的组合示例：**
```
gemini-2.5-pro-maxthinking-search  # thinking + search 组合
gemini-2.5-pro-nothinking-search   # nothinking + search 组合
假流式/gemini-2.5-pro-maxthinking-search
流式抗截断/gemini-2.5-pro-nothinking-search
```

**我们的现状：**
```python
# 我们当前的实现（简化版）
for thinking_suffix in ["-maxthinking", "-nothinking", "-search"]:
    models.append(f"{base_model}{thinking_suffix}")
    models.append(f"假流式/{base_model}{thinking_suffix}")
    models.append(f"流式抗截断/{base_model}{thinking_suffix}")
```

**差异：**
- ❌ 我们不支持多后缀组合（如 `-maxthinking-search`）
- ❌ 我们把 `-search` 和 thinking 后缀放在同一个列表里，无法组合
- ✅ 但我们有 Antigravity 模型（原项目没有）

**建议：** 参考原项目的组合逻辑，支持 `-maxthinking-search` 等组合后缀。

---

### 4. 自动封禁错误码配置

| 项目 | 默认封禁错误码 | 说明 |
|------|---------------|------|
| **原项目** | `[403]` | 仅封禁权限错误 |
| **我们** | `[401, 403, 404]` | 更严格的封禁策略 |

**我们的配置更严格**，这是合理的，因为：
- `401`: 认证失败（凭证无效）
- `403`: 权限不足（账号被封禁）
- `404`: 模型不存在或无权限（账号无权访问该模型）

**结论：** 我们的配置更合理，保持现状即可。

---

### 5. 思维链返回控制

**原项目新增配置：**

```python
async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    控制是否将思维链返回到前端。
    启用后，思维链会在响应中返回；禁用后，思维链会在响应中被过滤掉。

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    TOML config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))
```

**我们的现状：**
- ❌ 没有这个配置项
- 思维链总是返回到前端

**建议：** 添加此配置，让用户可以选择是否返回思维链内容（某些场景下用户可能不需要看到思维过程）。

---

### 6. 429 重试策略优化

**原项目的 429 重试间隔说明：**

```python
async def get_retry_429_interval() -> float:
    """Get 429 retry base delay in seconds (for exponential backoff).
    
    Note: This is now used as the BASE delay for exponential backoff.
    Actual delay = base_delay * (2 ** attempt)
    Example with base_delay=1.0: 1s, 2s, 4s, 8s...
    """
```

**我们的现状：**
```python
async def get_retry_429_interval() -> float:
    """Get 429 retry base delay in seconds (for exponential backoff).
    
    Note: This is now used as the BASE delay for exponential backoff.
    Actual delay = base_delay * (2 ** attempt)
    Example with base_delay=1.0: 1s, 2s, 4s, 8s...
    """
```

**差异：**
- 原项目明确说明使用**指数退避**（exponential backoff）
- 我们的文档也有说明，需要检查实现是否一致

**建议：** 检查我们的实现是否使用了指数退避，确保与文档一致。

---

## 🌟 我们独有的功能优势

### 1. Antigravity 模块（Gemini 3.0 API）

**完整的 Antigravity 支持：**
- ✅ `antigravity/client.py` - Antigravity API 客户端
- ✅ `antigravity/converter.py` - 格式转换器
- ✅ `src/antigravity_credential_manager.py` - 凭证管理
- ✅ `src/antigravity_usage_stats.py` - 使用统计
- ✅ ANT/ 前缀模型路由

**支持的 Antigravity 模型：**
```python
ANTIGRAVITY_BASE_MODELS = [
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-thinking",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-thinking",
    "gemini-2.5-computer-use-preview-10-2025",
    "gemini-3-pro-high",
    "gemini-3-pro-low",
    "gpt-oss-120b-medium",
]
```

**Antigravity 模型别名映射：**
```python
ANTIGRAVITY_MODEL_ALIAS = {
    "gemini-2.5-computer-use-preview-10-2025": "rev19-uic3-1p",
}
```

**设计原则：**
1. Antigravity API 本身已经是流式的，不需要"假流式"前缀
2. Antigravity API 有自己的续写机制，不需要"流式抗截断"前缀
3. 使用 ANT/ 前缀标识，便于区分来源和路由
4. 保持原始模型名称，不添加额外的功能后缀

---

### 2. IP 管理模块

- ✅ `src/ip_manager.py` - IP 白名单/黑名单管理
- 原项目没有此功能
- 适用于需要访问控制的部署场景

---

### 3. 管理员密码独立配置

```python
async def get_admin_password() -> str:
    """
    Get admin password setting for config management.

    Environment variable: ADMIN_PASSWORD
    TOML config key: admin_password
    Default: adm123
    """
```

**用途：** 用于配置管理的独立密码，与 API 密码和面板密码分离。

---

### 4. 备份管理

```python
async def get_max_backup_count() -> int:
    """
    Get maximum backup files count setting.
    When backup files exceed this count, oldest backups will be deleted.

    Environment variable: MAX_BACKUP_COUNT
    TOML config key: max_backup_count
    Default: 10
    """
```

**用途：** 自动管理备份文件数量，防止备份文件过多占用存储空间。

---

### 5. Antigravity 端点降级

```python
async def get_antigravity_api_endpoint_backup() -> str:
    """
    Get Antigravity backup API endpoint for failover.

    用于 Antigravity 端点降级的备用 API 端点。
    当主端点失败时（429/5xx/网络错误），自动切换到备用端点。

    Environment variable: ANTIGRAVITY_API_ENDPOINT_BACKUP
    TOML config key: antigravity_api_endpoint_backup
    Default: 空字符串（不启用备用端点）
    """
```

**用途：** 提高 Antigravity API 的可用性，主端点失败时自动切换。

---

## 📋 可借鉴的改进建议

### 优先级 1：高价值改进（推荐实施）

#### 1. 添加思维链返回控制

**实施方案：**

在 `config.py` 中添加：
```python
async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    控制是否将思维链返回到前端。
    启用后，思维链会在响应中返回；禁用后，思维链会在响应中被过滤掉。

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    TOML config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))
```

在响应处理逻辑中使用此配置过滤思维链内容。

**预期收益：**
- 用户可以选择是否接收思维链
- 减少不必要的数据传输
- 提升某些场景下的响应速度

---

#### 2. 支持模型后缀组合

**实施方案：**

修改 `config.py` 中的 `get_available_models()` 函数：

```python
def get_available_models(router_type="openai"):
    """
    Get available models with feature prefixes.
    """
    models = []

    # 1. GeminiCLI 模型（带功能前缀）
    for base_model in BASE_MODELS:
        # 基础模型
        models.append(base_model)

        if base_model in PUBLIC_API_MODELS:
            continue

        # 假流式模型
        models.append(f"假流式/{base_model}")

        # 流式抗截断模型
        models.append(f"流式抗截断/{base_model}")

        # 支持thinking模式后缀与功能前缀组合
        thinking_suffixes = ["-maxthinking", "-nothinking"]
        search_suffix = "-search"

        # 1. 单独的 thinking 后缀
        for thinking_suffix in thinking_suffixes:
            models.append(f"{base_model}{thinking_suffix}")
            models.append(f"假流式/{base_model}{thinking_suffix}")
            models.append(f"流式抗截断/{base_model}{thinking_suffix}")

        # 2. 单独的 search 后缀
        models.append(f"{base_model}{search_suffix}")
        models.append(f"假流式/{base_model}{search_suffix}")
        models.append(f"流式抗截断/{base_model}{search_suffix}")

        # 3. thinking + search 组合后缀
        for thinking_suffix in thinking_suffixes:
            combined_suffix = f"{thinking_suffix}{search_suffix}"
            models.append(f"{base_model}{combined_suffix}")
            models.append(f"假流式/{base_model}{combined_suffix}")
            models.append(f"流式抗截断/{base_model}{combined_suffix}")

    # 2. Antigravity 模型（ANT/ 前缀）
    models.extend(get_antigravity_models())

    return models
```

**预期收益：**
- 支持更灵活的模型配置
- 用户可以同时使用 thinking 和 search 功能
- 与原项目保持一致性

---

### 优先级 2：可选改进（按需实施）

#### 3. 添加 Postgres 存储支持

**实施方案：**

1. 在 `src/storage/` 目录下创建 `postgres_storage_manager.py`
2. 实现 `PostgresStorageManager` 类
3. 在 `storage_adapter.py` 中添加 Postgres 优先级检测

**适用场景：**
- 用户需要关系型数据库
- 需要复杂查询和事务支持
- 已有 Postgres 基础设施

**预期收益：**
- 提供更多存储选择
- 支持更复杂的数据查询
- 更好的数据一致性保证

---

#### 4. 优化 429 重试策略（指数退避）

**实施方案：**

检查当前 429 重试实现，确保使用指数退避：

```python
# 伪代码示例
base_delay = await get_retry_429_interval()  # 默认 1.0 秒
for attempt in range(max_retries):
    try:
        # 执行请求
        pass
    except RateLimitError:
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)  # 指数退避
            await asyncio.sleep(delay)
        else:
            raise
```

更新 `config.py` 中的文档说明（已完成）。

**预期收益：**
- 更智能的重试策略
- 减少对 API 的压力
- 提高成功率

---

## 🔄 更新检查清单

### 立即实施（高优先级）
- [ ] 添加 `get_return_thoughts_to_frontend()` 配置
- [ ] 支持模型后缀组合（thinking + search）
- [ ] 更新 `.env.example` 添加新配置项

### 按需实施（中优先级）
- [ ] 检查并优化 429 重试策略（确认是否使用指数退避）
- [ ] 更新文档说明重试策略

### 长期规划（低优先级）
- [ ] 考虑添加 Postgres 存储支持（如有需求）
- [ ] 同步原项目的其他小改进

---

## 📝 总结

### 我们的核心优势

| 功能 | 我们 | 原项目 | 说明 |
|------|------|--------|------|
| **Antigravity 模块** | ✅ | ❌ | 我们独有，支持 Gemini 3.0 和 Claude |
| **IP 管理** | ✅ | ❌ | 我们独有，访问控制功能 |
| **管理员密码** | ✅ | ❌ | 我们独有，独立的配置管理密码 |
| **备份管理** | ✅ | ❌ | 我们独有，自动清理旧备份 |
| **Antigravity 降级** | ✅ | ❌ | 我们独有，备用端点支持 |
| **更严格的封禁** | ✅ | ⚠️ | 我们封禁更多错误码 |
| **flash-lite 模型** | ✅ | ❌ | 我们独有 |

### 可以借鉴的改进

| 功能 | 优先级 | 实施难度 | 预期收益 |
|------|--------|---------|---------|
| **思维链返回控制** | 🔴 高 | 🟢 低 | 🟢 高 |
| **模型后缀组合** | 🔴 高 | 🟡 中 | 🟢 高 |
| **429 指数退避** | 🟡 中 | 🟢 低 | 🟡 中 |
| **Postgres 支持** | 🟢 低 | 🔴 高 | 🟡 中 |

### 最终结论

**我们的魔改版功能更丰富，Antigravity 模块是核心竞争力。** 原项目的一些小改进（思维链控制、模型后缀组合）值得借鉴，但不影响我们的核心优势。

**推荐行动：**
1. 优先实施思维链返回控制和模型后缀组合
2. 检查 429 重试策略是否已使用指数退避
3. Postgres 支持可以作为长期规划，按需实施

---

**文档更新时间：** 2024-12-04  
**下次检查时间：** 建议每月检查一次上游更新
#### 1. **AUTO_BAN_ERROR_CODES 默认值不同**
   - **原作者**: `[403]`
   - **我们的版本**: `[401, 403, 404]`
   - **建议**: 保持我们的配置，因为我们增加了 401 和 404 的处理更加完善

#### 2. **Antigravity 功能**
   - **我们的版本**: 包含完整的 Antigravity API 支持
   - **原作者**: 没有 Antigravity 相关功能
   - **建议**: 保持我们的扩展功能

#### 3. **retry_429_interval 注释差异**
   - **我们的版本**: 明确说明使用指数退避策略
   - **原作者**: 没有详细说明
   - **建议**: 保持我们的详细注释

---

## 推荐行动

### 立即执行
1. **对比缓存管理器代码**: 检查 `src/storage/cache_manager.py` 和 `src/storage/file_storage_manager.py`，看是否需要同步原作者的优化

### 可选执行
2. **配置项审查**: 确认 `get_return_thoughts_to_frontend()` 是否在我们的代码中使用
3. **性能测试**: 如果同步了缓存优化，进行性能测试确保改进有效

### 保持现状
- AUTO_BAN_ERROR_CODES 配置（我们的更完善）
- Antigravity 功能（我们的独有扩展）
- 详细的代码注释（我们的更清晰）

---

## 总结

原作者最近的更新主要集中在**性能优化**（缓存系统）和**代码清理**。我们的版本在功能上更加完善（Antigravity 支持、更全面的错误处理），但可以考虑同步原作者的缓存优化逻辑以提升性能。


---

## 详细技术对比：缓存优化

### 原作者的缓存管理器 (优化后)

```python
class UnifiedCacheManager:
    def __init__(self, cache_backend, write_delay=1.0, max_write_delay=30.0, 
                 min_write_interval=5.0, name="cache"):
        # ✅ 移除了 cache_ttl 参数
        self._cache_loaded = False  # ✅ 新增：标记是否已加载
        self._last_write_time = 0   # ✅ 新增：上次写入时间
        self._pending_write_time = 0  # ✅ 新增：待写入时间戳
        self._initial_load_count = 0  # ✅ 新增：启动加载次数统计
        self._write_backend_count = 0  # ✅ 新增：后端写入次数统计
        
    async def get(self, key, default=None):
        # ✅ 只在首次加载，之后不再从磁盘读取
        if not self._cache_loaded:
            await self._load_initial_cache()
        return self._cache.get(key, default)
    
    async def _load_initial_cache(self):
        # ✅ 只在启动时调用一次
        if self._cache_loaded:
            return  # 已加载，直接返回
        
        data = await self._backend.load_data()
        self._cache = data
        self._cache_loaded = True  # 标记已加载
        self._initial_load_count += 1
        
    def _should_write_now(self, current_time):
        # ✅ 智能判断是否应该写入
        # 1. 检查最小写入间隔（避免频繁写入）
        if current_time - self._last_write_time < self._min_write_interval:
            return False
        
        # 2. 超过最大延迟必须写入
        time_since_dirty = current_time - self._pending_write_time
        if time_since_dirty >= self._max_write_delay:
            return True
        
        # 3. 超过初始延迟可以写入
        if time_since_dirty >= self._write_delay:
            return True
        
        return False
```

### 我们当前的缓存管理器 (待优化)

```python
class UnifiedCacheManager:
    def __init__(self, cache_backend, cache_ttl=300.0, write_delay=1.0, name="cache"):
        # ❌ 使用 cache_ttl，会定期重新加载
        self._cache_ttl = cache_ttl
        self._last_cache_time = 0  # ❌ 用于 TTL 检查
        # ❌ 缺少智能写入策略相关字段
        
    async def get(self, key, default=None):
        # ❌ 每次都检查 TTL，可能触发磁盘读取
        await self._ensure_cache_loaded()
        return self._cache.get(key, default)
    
    async def _ensure_cache_loaded(self):
        current_time = time.time()
        # ❌ 问题：即使缓存脏了，TTL 过期也会重新加载
        if self._last_cache_time == 0 or (
            current_time - self._last_cache_time > self._cache_ttl 
            and not self._cache_dirty  # 只有这个条件保护
        ):
            await self._load_cache()  # ❌ 可能频繁从磁盘读取
            self._last_cache_time = current_time
```

### 性能对比

| 指标 | 我们的版本 | 原作者版本 | 改进 |
|------|-----------|-----------|------|
| 启动后磁盘读取次数 | 可能多次（每5分钟） | 1次 | ✅ 减少磁盘I/O |
| 写入策略 | 固定延迟 | 智能延迟 | ✅ 更灵活 |
| 数据一致性 | TTL可能导致问题 | 内存为准 | ✅ 更可靠 |
| 性能监控 | 基础统计 | 详细统计 | ✅ 更易调试 |

### 缓存管理器缓存什么？（从源码分析）

**缓存管理器管理两个文件**：

1. **`creds.toml`** - 凭证缓存管理器 (`_credentials_cache_manager`)
   - 凭证数据：`refresh_token`, `client_id`, `client_secret`, `token`, `token_uri`, `project_id`, `scopes`, `expiry`, `access_token` 等
   - 状态数据：`disabled`, `error_codes`, `last_success`, `user_email`
   - 使用限额：`gemini_2_5_pro_calls`, `total_calls`, `next_reset_time`, `daily_limit_gemini_2_5_pro`, `daily_limit_total`
   - 冻结状态：`freeze_frozen`, `freeze_time`, `freeze_is_owner`, `freeze_delete_reason`, `freeze_auto_delete_time`

2. **`config.toml`** - 配置缓存管理器 (`_config_cache_manager`)
   - 密码配置：`api_password`, `panel_password`, `admin_password`
   - 端点配置：`code_assist_endpoint`, `oauth_proxy_url`, `googleapis_proxy_url` 等
   - 功能配置：`auto_ban_enabled`, `auto_ban_error_codes`, `calls_per_rotation`, `retry_429_enabled` 等
   - Antigravity 配置：`antigravity_api_endpoint`, `antigravity_models_endpoint` 等
   - 备份配置：`backup.enabled`, `backup.github_token`, `backup.github_repo` 等

**注意**：`model_credits.toml` 是模型价格配置文件，**不经过缓存管理器**，与缓存优化无关。

**缓存优化的实际影响**：
- 每次 API 请求都需要读取凭证状态（选择可用凭证）
- 每次请求后需要更新凭证状态（调用次数、错误码等）
- 配置读取（密码验证、端点获取等）
- 这些操作在高并发场景下非常频繁，缓存优化可以显著减少磁盘 I/O

### 为什么原作者的方案更好？

1. **减少磁盘 I/O**: 启动后只读取 1 次 `creds.toml` 和 `config.toml`，之后完全在内存操作
2. **避免数据不一致**: 内存缓存是唯一真实来源，不会因为 TTL 过期重新加载旧数据
3. **智能写入**: 既避免频繁写入（最小间隔5秒），又防止数据丢失（最大延迟30秒）
4. **更好的监控**: 可以清楚看到磁盘操作次数，便于性能调优

**对我们的实际影响**：
- 主要优化凭证管理操作（添加/删除/更新凭证状态）
- 配置读取操作（系统配置项）
- 不影响使用统计（我们已经独立管理）

### 建议的同步优先级

**高优先级**（建议立即同步）:
- 移除 `cache_ttl` 机制
- 添加 `_cache_loaded` 标志
- 改造 `_ensure_cache_loaded()` 为 `_load_initial_cache()`

**中优先级**（可以后续优化）:
- 添加智能延迟写入策略
- 增加性能监控统计

### 实际收益评估

**场景 1: 频繁查询凭证状态**（如轮换凭证时）
- 当前：每 5 分钟可能触发 1 次磁盘读取
- 优化后：启动后 0 次磁盘读取
- **收益**: 中等（取决于运行时长）

**场景 2: 频繁更新凭证状态**（如禁用/启用凭证）
- 当前：固定 0.5 秒延迟写入
- 优化后：智能延迟（5-30 秒），减少磁盘写入次数
- **收益**: 高（减少磁盘 I/O）

**场景 3: 长时间运行的服务**
- 当前：每 5 分钟读取 1 次 × 24 小时 = 288 次磁盘读取/天
- 优化后：启动时读取 1 次
- **收益**: 非常高

**总结**: 如果你的服务是长时间运行的（比如 24/7 运行），这个优化非常值得同步。

---

## 🔴 内存问题分析（2025-12-04）

### 当前内存状态
- 系统总内存: 898 MB
- 已用内存: 660 MB (73.47%)
- 可用内存: ~238 MB

### 已排除的泄漏点（代码已优化）

1. **deque maxlen 已优化为 1**
   - `src/storage/cache_manager.py`: `deque(maxlen=1)` ✅
   - `src/storage/redis_manager.py`: `deque(maxlen=1)` ✅
   - `src/storage/mongodb_manager.py`: `deque(maxlen=1)` ✅
   - `src/storage/postgres_manager.py`: `deque(maxlen=1)` ✅

2. **WebSocket 连接管理器已限制**
   - `src/web_routes.py`: `deque(maxlen=3)` ✅
   - 有自动清理死连接机制 ✅

3. **httpx 客户端正确关闭**
   - `antigravity/client.py`: 使用 `async with` 正确管理 ✅
   - `src/google_chat_api.py`: 流式响应有 `finally` 清理 ✅

### 可能的内存增长原因

1. **creds.toml 文件过大**
   - 你的 `creds.toml` 有 **18533 行**！
   - 这意味着有大量凭证数据被加载到内存
   - 每个凭证包含：token、refresh_token、状态、使用统计等
   - **建议**: 清理不再使用的凭证（disabled=true 且长时间未使用的）

2. **IP 管理器缓存**
   - `src/ip_manager.py` 会记录每个 IP 的访问历史
   - 包括：`user_agents`（最多10个）、`models_used`、`endpoints` 等
   - 如果有大量不同 IP 访问，缓存会持续增长
   - **建议**: 运行 `memory_diagnostic.py` 查看 IP 记录数量

3. **Python GC 未及时回收**
   - 流式响应中有 `gc.collect()` 调用，但可能不够频繁
   - **建议**: 增加 GC 调用频率或使用 `gc.set_threshold()`

### 建议的排查步骤

```bash
# 1. 在服务器上运行内存诊断
python memory_diagnostic.py

# 2. 查看 creds.toml 中的凭证数量
grep -c '^\[' creds/creds.toml

# 3. 查看 IP 记录数量
grep -c '"ip":' creds/ip_data.json  # 如果有这个文件

# 4. 监控内存变化
watch -n 5 'ps aux | grep python | head -5'
```

### 深入分析：可能的内存泄漏点

#### 1. **后台任务未被 TaskManager 管理**（中风险）

`web.py` 中有两个后台任务直接使用 `asyncio.create_task()` 而不是 `task_manager.create_task()`：

```python
# web.py:58 - 未被管理
asyncio.create_task(load_env_creds())

# web.py:65 - 未被管理
asyncio.create_task(start_freeze_checker())
```

**问题**：这些任务在应用关闭时可能不会被正确取消，导致资源泄漏。

**修复建议**：
```python
from src.task_manager import create_managed_task
create_managed_task(load_env_creds(), name="load_env_creds")
create_managed_task(start_freeze_checker(), name="freeze_checker")
```

#### 2. **IP 管理器的后台任务**（中风险）

`src/ip_manager.py` 中的后台任务也未被管理：

```python
# ip_manager.py:59-62
asyncio.create_task(self._periodic_save())
asyncio.create_task(self._periodic_unban_check())
```

**问题**：这些任务是无限循环，如果 IP 管理器被多次初始化，会创建多个后台任务。

#### 3. **流式响应客户端泄漏**（低风险，已有清理）

`src/google_chat_api.py` 中的流式客户端在大多数路径都有 `client.aclose()` 调用，但如果请求被客户端中断（如用户关闭浏览器），可能不会触发 `finally` 块。

#### 4. **禁用凭证也被加载到内存**（高风险 ⭐）

**问题确认**：缓存管理器会把**整个 `creds.toml` 加载到内存**，包括所有禁用的凭证！

```python
# src/storage/cache_manager.py:_load_cache()
data = await self._backend.load_data()  # 加载整个文件
self._cache = data  # 全部存入内存
```

**你的情况**：
- 总凭证：1038 个
- 禁用凭证：424 个（41%）
- 每个凭证包含：`refresh_token`, `access_token`, `client_id`, `client_secret`, `scopes`, `expiry`, `project_id`, `user_email`, `error_codes`, `disabled`, `last_success`, `gemini_2_5_pro_calls`, `total_calls`, `next_reset_time`, `daily_limit_*` 等 15+ 个字段

**内存估算**：
- 每个凭证在 Python 字典中约占 **3-5 KB**（字符串 + 字典开销）
- 1038 个凭证 × 4 KB ≈ **4 MB** 凭证数据
- 424 个禁用凭证 × 4 KB ≈ **1.7 MB** 浪费的内存

**但这不是主要问题**，1.7 MB 不足以解释 200 MB 的差距。

#### 5. **Python 基础内存开销**

- Python 运行时：~50-80 MB
- FastAPI + Starlette：~20-30 MB
- httpx + h2：~10-20 MB
- toml + aiofiles 等：~5-10 MB
- **基础开销约 100-150 MB**

#### 6. **真正的内存大户可能是**

- **httpx 连接池**：每个活跃连接约 1-5 MB
- **未完成的流式响应**：如果用户中断请求，生成器可能不会被清理
- **Python 对象碎片**：长时间运行后内存碎片化

### 快速优化建议

1. **修复后台任务管理**（高优先级）
   ```python
   # 在 web.py 中使用 task_manager
   from src.task_manager import create_managed_task
   create_managed_task(load_env_creds(), name="load_env_creds")
   ```

2. **添加定期 GC**（中优先级）
   ```python
   # 在 web.py 的 lifespan 中添加
   import gc
   gc.set_threshold(700, 10, 5)  # 更激进的 GC
   ```

3. **监控内存变化**（诊断用）
   ```bash
   # 在服务器上运行
   python memory_diagnostic.py
   
   # 或者监控容器内存
   docker stats 2apifare --no-stream
   ```

4. **设置容器内存限制**（保护措施）
   ```yaml
   # docker-compose.yml
   services:
     2apifare:
       mem_limit: 400m
       memswap_limit: 500m
   ```

5. **清理禁用凭证**（可选优化）
   - 当前 424 个禁用凭证占用约 1.7 MB
   - 可以考虑定期清理长期禁用的凭证
   - 或者修改缓存逻辑，只加载启用的凭证（需要较大改动）

### 下一步排查建议

1. **在服务器上运行内存诊断**：
   ```bash
   docker exec -it 2apifare python memory_diagnostic.py
   ```

2. **监控内存变化**：
   ```bash
   # 每 5 秒记录一次内存
   while true; do docker stats 2apifare --no-stream >> memory_log.txt; sleep 5; done
   ```

3. **检查是否有内存持续增长**：
   - 如果内存稳定在 350 MB 左右，可能只是 Python 的正常开销
   - 如果内存持续增长，说明有泄漏

4. **重启后观察**：
   ```bash
   docker restart 2apifare
   # 观察重启后内存是否回到较低水平
   ```
