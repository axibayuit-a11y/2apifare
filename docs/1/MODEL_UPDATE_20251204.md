# 模型更新 - 2025年12月4日

## 变更说明

Google 于 2025年12月4日 移除了 `gemini-2.5-pro-preview-06-05` 系列模型，并推出了新的 `latest` 系列模型。

## 移除的模型

以下模型已被 Google 移除，不再可用：

- ❌ `gemini-2.5-pro-preview-06-05`
- ❌ `gemini-2.5-pro-preview-06-05-maxthinking`
- ❌ `gemini-2.5-pro-preview-06-05-nothinking`
- ❌ `gemini-2.5-pro-preview-06-05-search`

**症状**：使用这些模型会返回 404 错误 "Requested entity was not found"

## 新增的模型

### Gemini Flash Latest 系列

自动更新到最新的 Flash 版本：

- ✅ `gemini-flash-latest`
- ✅ `gemini-flash-latest-maxthinking`
- ✅ `gemini-flash-latest-nothinking`
- ✅ `gemini-flash-latest-search`

**特点**：
- 自动跟随 Google 最新的 Flash 模型版本
- 无需手动更新模型名称
- 性能和功能持续改进

### Gemini Flash Lite Latest 系列

自动更新到最新的 Flash Lite 版本：

- ✅ `gemini-flash-lite-latest`
- ✅ `gemini-flash-lite-latest-maxthinking`
- ✅ `gemini-flash-lite-latest-nothinking`
- ✅ `gemini-flash-lite-latest-search`

**特点**：
- 超轻量级模型
- 成本最低
- 适合简单任务

## 迁移建议

### 从 0605 迁移到 Latest

| 旧模型 | 推荐替代 | 说明 |
|--------|---------|------|
| `gemini-2.5-pro-preview-06-05` | `gemini-flash-latest` | Flash Latest 性能接近，成本更低 |
| `gemini-2.5-pro-preview-06-05-maxthinking` | `gemini-flash-latest-maxthinking` | 保持 thinking 功能 |
| `gemini-2.5-pro-preview-06-05-search` | `gemini-flash-latest-search` | 保持搜索功能 |

### 其他可用模型

如果需要更强的性能，可以使用：

**GeminiCLI 模型**：
- `gemini-3-pro-preview` - 最强 CLI 模型
- `gemini-2.5-pro` - 中级模型，性价比高
- `gemini-2.5-flash` - 轻量快速
- `gemini-2.5-flash-lite` - 超轻量

**Antigravity 模型**（需要 ANT/ 前缀）：
- `ANT/gemini-3-pro-high` - 最强模型
- `ANT/claude-sonnet-4-5` - Claude 高级模型
- `ANT/gemini-2.5-flash` - 轻量快速

## 配置文件变更

### config.py

```python
BASE_MODELS = [
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",        # 新增
    "gemini-flash-lite-latest",   # 新增
]
```

### creds/model_credits.toml

```toml
# 新增 Flash Latest 系列
"gemini-flash-latest" = 0.35
"gemini-flash-latest-maxthinking" = 0.4
"gemini-flash-latest-nothinking" = 0.3
"gemini-flash-latest-search" = 0.45

# 新增 Flash Lite Latest 系列
"gemini-flash-lite-latest" = 0.25
"gemini-flash-lite-latest-maxthinking" = 0.30
"gemini-flash-lite-latest-nothinking" = 0.20
"gemini-flash-lite-latest-search" = 0.35
```

## 使用示例

### OpenAI 兼容 API

```bash
curl http://localhost:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash-latest",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### 带搜索功能

```bash
curl http://localhost:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash-latest-search",
    "messages": [{"role": "user", "content": "最新的AI新闻"}]
  }'
```

### 带 Thinking 功能

```bash
curl http://localhost:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash-latest-maxthinking",
    "messages": [{"role": "user", "content": "解决这个数学问题"}]
  }'
```

## 注意事项

1. **重启服务**：修改配置后需要重启服务才能生效
2. **客户端更新**：如果客户端硬编码了 0605 模型名称，需要更新
3. **积分计算**：新模型的积分与 Flash 系列相同
4. **自动更新**：Latest 系列会自动跟随 Google 更新，无需手动维护

## 故障排查

### 问题：仍然收到 404 错误

**原因**：
1. 服务未重启
2. 使用了已移除的 0605 模型
3. 客户端缓存了旧的模型列表

**解决方案**：
1. 重启服务：`Ctrl+C` 停止，然后重新运行
2. 更新模型名称为 `gemini-flash-latest`
3. 清除客户端缓存

### 问题：模型列表中看不到新模型

**原因**：配置文件未更新或服务未重启

**解决方案**：
1. 确认 `config.py` 中已添加新模型
2. 重启服务
3. 访问 `/v1/models` 查看模型列表

## 相关链接

- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API 文档](https://ai.google.dev/docs)
- [项目 GitHub](https://github.com/axibayuit-a11y/2apifare)
