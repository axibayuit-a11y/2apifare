"""
OpenAI Router - Handles OpenAI format API requests
处理OpenAI格式请求的路由模块
"""

import json
import time
import uuid
import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import (
    get_available_models,
    is_fake_streaming_model,
    is_anti_truncation_model,
    is_antigravity_model,
    get_antigravity_base_model,
    get_base_model_from_feature_model,
    get_anti_truncation_max_attempts,
)
from log import log
from .anti_truncation import apply_anti_truncation_to_stream
from .credential_manager import CredentialManager
from .google_chat_api import send_gemini_request
from .models import ChatCompletionRequest, ModelList, Model
from .task_manager import create_managed_task
from .openai_transfer import (
    openai_request_to_gemini_payload,
    gemini_response_to_openai,
    gemini_stream_chunk_to_openai,
    _convert_usage_metadata,
)

# 创建路由器
router = APIRouter()
security = HTTPBearer()

# 全局凭证管理器实例
credential_manager = None


@asynccontextmanager
async def get_credential_manager():
    """获取全局凭证管理器实例"""
    global credential_manager
    if not credential_manager:
        credential_manager = CredentialManager()
        await credential_manager.initialize()
    yield credential_manager


# ============ Antigravity 辅助函数（错误处理和重试）============


def _extract_error_code_from_exception(error_message: str) -> int:
    """从异常消息中提取 HTTP 错误码

    Args:
        error_message: 异常消息字符串

    Returns:
        int: HTTP 错误码，如果无法识别则返回 None

    Note:
        使用字符串匹配识别错误码，未来可优化为从实际 HTTP 响应中提取
    """
    if "403" in error_message or "403 Forbidden" in error_message:
        return 403
    elif "401" in error_message or "401 Unauthorized" in error_message:
        return 401
    elif "404" in error_message:
        return 404
    elif "429" in error_message:
        return 429
    elif "500" in error_message:
        return 500
    elif "400" in error_message or "400 Bad Request" in error_message:
        return 400
    return None


async def _check_should_retry_antigravity(error_code: int, auto_ban_error_codes: list) -> bool:
    """检查 Antigravity 错误是否应该重试

    Args:
        error_code: HTTP 错误码
        auto_ban_error_codes: 自动封禁的错误码列表

    Returns:
        bool: True 表示应该重试，False 表示不重试
    """
    if error_code is None:
        return False
    return error_code in auto_ban_error_codes


async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """验证用户密码"""
    from config import get_api_password

    password = await get_api_password()
    token = credentials.credentials
    if token != password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token


@router.get("/v1/models", response_model=ModelList)
async def list_models():
    """返回OpenAI格式的模型列表"""
    models = get_available_models("openai")
    return ModelList(data=[Model(id=m) for m in models])


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, token: str = Depends(authenticate)):
    """处理OpenAI格式的聊天完成请求"""

    # ========== IP 拦截和记录 ==========
    # 获取客户端 IP
    client_ip = request.client.host if request.client else "unknown"

    # 检查 X-Forwarded-For 和 X-Real-IP 头（支持反向代理）
    forwarded_for = request.headers.get("X-Forwarded-For")
    real_ip = request.headers.get("X-Real-IP")

    if forwarded_for:
        # X-Forwarded-For 可能包含多个 IP，取第一个
        client_ip = forwarded_for.split(",")[0].strip()
    elif real_ip:
        client_ip = real_ip.strip()

    # ========== 禁止 IPv6 请求 ==========
    # 检测 IPv6 地址（包含冒号）
    if ":" in client_ip:
        log.warning(f"Blocked IPv6 request from: {client_ip}")
        raise HTTPException(
            status_code=403,
            detail="IPv6 requests are not supported. Please use IPv4 to access this service."
        )

    # 获取 User-Agent
    user_agent = request.headers.get("User-Agent", "unknown")

    # 获取原始请求数据
    try:
        raw_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # 创建请求对象
    try:
        request_data = ChatCompletionRequest(**raw_data)
    except Exception as e:
        log.error(f"Request validation failed: {e}")
        raise HTTPException(status_code=400, detail=f"Request validation error: {str(e)}")

    # ========== IP 拦截检查（仅检查，不记录） ==========
    try:
        from .ip_manager import get_ip_manager

        ip_manager = await get_ip_manager()

        # 仅检查 IP 是否被封禁或限速（不记录请求）
        allowed, reject_reason = await ip_manager.check_ip_allowed(ip=client_ip)

        if not allowed:
            log.warning(f"Blocked request from IP: {client_ip}, reason: {reject_reason}")
            if reject_reason == "banned":
                raise HTTPException(status_code=403, detail="禁止访问，请翻译具体报错信息查看原因，或联系服务商询问被禁止原因")
            elif reject_reason == "rate_limited":
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
            else:
                raise HTTPException(status_code=403, detail="访问被拒绝")

    except HTTPException:
        raise
    except Exception as e:
        # IP 管理器出错不应该影响正常请求，只记录错误
        log.error(f"IP manager error: {e}")

    # 健康检查
    if (
        len(request_data.messages) == 1
        and getattr(request_data.messages[0], "role", None) == "user"
        and getattr(request_data.messages[0], "content", None) == "Hi"
    ):
        return JSONResponse(
            content={
                "choices": [{"message": {"role": "assistant", "content": "gcli2api正常工作中"}}]
            }
        )

    # 限制max_tokens
    if getattr(request_data, "max_tokens", None) is not None and request_data.max_tokens > 65535:
        request_data.max_tokens = 65535

    # 覆写 top_k 为 64
    setattr(request_data, "top_k", 64)

    # 过滤空消息
    filtered_messages = []
    for m in request_data.messages:
        content = getattr(m, "content", None)
        if content:
            if isinstance(content, str) and content.strip():
                filtered_messages.append(m)
            elif isinstance(content, list) and len(content) > 0:
                has_valid_content = False
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text", "").strip():
                            has_valid_content = True
                            break
                        elif part.get("type") == "image_url" and part.get("image_url", {}).get(
                            "url"
                        ):
                            has_valid_content = True
                            break
                if has_valid_content:
                    filtered_messages.append(m)

    request_data.messages = filtered_messages

    # 处理模型名称和功能检测
    model = request_data.model

    # 检测是否是 Antigravity 模型（ANT/ 前缀）
    if is_antigravity_model(model):
        log.info(f"Detected Antigravity model: {model}")
        ant_response = await handle_antigravity_request(request_data)

        # 记录成功的IP请求
        try:
            await ip_manager.record_request(
                ip=client_ip, endpoint="/v1/chat/completions", user_agent=user_agent, model=model
            )
        except Exception as e:
            log.error(f"Failed to record IP request: {e}")

        return ant_response

    # GeminiCLI 模型处理逻辑
    use_fake_streaming = is_fake_streaming_model(model)
    use_anti_truncation = is_anti_truncation_model(model)

    # 获取基础模型名
    real_model = get_base_model_from_feature_model(model)
    request_data.model = real_model

    # 获取凭证管理器
    from src.credential_manager import get_credential_manager

    cred_mgr = await get_credential_manager()

    # 获取有效凭证
    credential_result = await cred_mgr.get_valid_credential()
    if not credential_result:
        log.error("当前无可用凭证，请去控制台获取")
        raise HTTPException(status_code=500, detail="当前无可用凭证，请去控制台获取")

    current_file = credential_result
    log.debug(f"Using credential: {current_file}")

    # 增加调用计数
    cred_mgr.increment_call_count()

    # 转换为Gemini API payload格式
    try:
        api_payload = await openai_request_to_gemini_payload(request_data)
    except Exception as e:
        log.error(f"OpenAI to Gemini conversion failed: {e}")
        raise HTTPException(status_code=500, detail="Request conversion failed")

    # 处理假流式
    if use_fake_streaming and getattr(request_data, "stream", False):
        request_data.stream = False
        fake_stream_resp = await fake_stream_response(api_payload, cred_mgr)

        # 记录成功的IP请求
        try:
            model_name = getattr(request_data, "model", "unknown")
            await ip_manager.record_request(
                ip=client_ip, endpoint="/v1/chat/completions", user_agent=user_agent, model=model_name
            )
        except Exception as e:
            log.error(f"Failed to record IP request: {e}")

        return fake_stream_resp

    # 处理抗截断 (仅流式传输时有效)
    is_streaming = getattr(request_data, "stream", False)
    if use_anti_truncation and is_streaming:
        log.info("启用流式抗截断功能")
        max_attempts = await get_anti_truncation_max_attempts()

        # 使用流式抗截断处理器
        gemini_response = await apply_anti_truncation_to_stream(
            lambda api_payload: send_gemini_request(api_payload, is_streaming, cred_mgr),
            api_payload,
            max_attempts,
        )

        # 记录成功的IP请求
        try:
            model_name = getattr(request_data, "model", "unknown")
            await ip_manager.record_request(
                ip=client_ip, endpoint="/v1/chat/completions", user_agent=user_agent, model=model_name
            )
        except Exception as e:
            log.error(f"Failed to record IP request: {e}")

        return await convert_streaming_response(gemini_response, model)
    elif use_anti_truncation and not is_streaming:
        log.warning("抗截断功能仅在流式传输时有效，非流式请求将忽略此设置")

    # 发送请求（429重试已在google_api_client中处理）
    is_streaming = getattr(request_data, "stream", False)
    log.debug(f"Sending request: streaming={is_streaming}, model={real_model}")
    response = await send_gemini_request(api_payload, is_streaming, cred_mgr)

    # ========== 记录成功的IP请求 ==========
    # 只有AI成功响应后才记录请求次数
    try:
        model_name = getattr(request_data, "model", "unknown")
        await ip_manager.record_request(
            ip=client_ip, endpoint="/v1/chat/completions", user_agent=user_agent, model=model_name
        )
    except Exception as e:
        # IP记录失败不影响响应返回
        log.error(f"Failed to record IP request: {e}")

    # 如果是流式响应，直接返回
    if is_streaming:
        return await convert_streaming_response(response, model)

    # 转换非流式响应
    try:
        if hasattr(response, "body"):
            response_data = json.loads(
                response.body.decode() if isinstance(response.body, bytes) else response.body
            )
        else:
            response_data = json.loads(
                response.content.decode()
                if isinstance(response.content, bytes)
                else response.content
            )

        openai_response = gemini_response_to_openai(response_data, model)
        return JSONResponse(content=openai_response)

    except Exception as e:
        log.error(f"Response conversion failed: {e}")
        log.error(f"Response object: {response}")
        raise HTTPException(status_code=500, detail="Response conversion failed")


async def fake_stream_response(api_payload: dict, cred_mgr: CredentialManager) -> StreamingResponse:
    """处理假流式响应"""

    async def stream_generator():
        try:
            # 发送心跳
            heartbeat = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ]
            }
            yield f"data: {json.dumps(heartbeat)}\n\n".encode()

            # 异步发送实际请求
            async def get_response():
                return await send_gemini_request(api_payload, False, cred_mgr)

            # 创建请求任务
            response_task = create_managed_task(get_response(), name="openai_fake_stream_request")

            try:
                # 每3秒发送一次心跳，直到收到响应
                while not response_task.done():
                    await asyncio.sleep(3.0)
                    if not response_task.done():
                        yield f"data: {json.dumps(heartbeat)}\n\n".encode()

                # 获取响应结果
                response = await response_task

            except asyncio.CancelledError:
                # 取消任务并传播取消
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as e:
                # 取消任务并处理其他异常
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                log.error(f"Fake streaming request failed: {e}")
                raise

            # 发送实际请求
            # response 已在上面获取

            # 处理结果
            if hasattr(response, "body"):
                body_str = (
                    response.body.decode()
                    if isinstance(response.body, bytes)
                    else str(response.body)
                )
            elif hasattr(response, "content"):
                body_str = (
                    response.content.decode()
                    if isinstance(response.content, bytes)
                    else str(response.content)
                )
            else:
                body_str = str(response)

            try:
                response_data = json.loads(body_str)

                # 从Gemini响应中提取内容，使用思维链分离逻辑
                content = ""
                reasoning_content = ""
                if "candidates" in response_data and response_data["candidates"]:
                    # Gemini格式响应 - 使用思维链分离
                    from .openai_transfer import _extract_content_and_reasoning

                    candidate = response_data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        parts = candidate["content"]["parts"]
                        content, reasoning_content = _extract_content_and_reasoning(parts)
                elif "choices" in response_data and response_data["choices"]:
                    # OpenAI格式响应
                    content = response_data["choices"][0].get("message", {}).get("content", "")

                # 如果没有正常内容但有思维内容，给出警告
                if not content and reasoning_content:
                    log.warning("Fake stream response contains only thinking content")
                    content = "[模型正在思考中，请稍后再试或重新提问]"

                if content:
                    # 构建响应块，包括思维内容（如果有）
                    delta = {"role": "assistant", "content": content}
                    if reasoning_content:
                        delta["reasoning_content"] = reasoning_content

                    # 转换usageMetadata为OpenAI格式
                    usage = _convert_usage_metadata(response_data.get("usageMetadata"))

                    # 构建完整的OpenAI格式的流式响应块
                    content_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gcli2api-streaming",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}],
                    }

                    # 只有在有usage数据时才添加usage字段（确保在最后一个chunk中）
                    if usage:
                        content_chunk["usage"] = usage

                    yield f"data: {json.dumps(content_chunk)}\n\n".encode()
                else:
                    log.warning(f"No content found in response: {response_data}")
                    # 如果完全没有内容，提供默认回复
                    error_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gcli2api-streaming",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "[响应为空，请重新尝试]"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            except json.JSONDecodeError:
                error_chunk = {
                    "id": str(uuid.uuid4()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gcli2api-streaming",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": body_str},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()

            yield "data: [DONE]\n\n".encode()

        except Exception as e:
            log.error(f"Fake streaming error: {e}")
            error_chunk = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "gcli2api-streaming",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": f"Error: {str(e)}"},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


async def convert_streaming_response(gemini_response, model: str) -> StreamingResponse:
    """转换流式响应为OpenAI格式"""
    response_id = str(uuid.uuid4())

    async def openai_stream_generator():
        try:
            # 处理不同类型的响应对象
            if hasattr(gemini_response, "body_iterator"):
                # FastAPI StreamingResponse
                async for chunk in gemini_response.body_iterator:
                    if not chunk:
                        continue

                    # 处理不同数据类型的startswith问题
                    if isinstance(chunk, bytes):
                        if not chunk.startswith(b"data: "):
                            continue
                        payload = chunk[len(b"data: ") :]
                    else:
                        chunk_str = str(chunk)
                        if not chunk_str.startswith("data: "):
                            continue
                        payload = chunk_str[len("data: ") :].encode()
                    try:
                        gemini_chunk = json.loads(payload.decode())
                        openai_chunk = gemini_stream_chunk_to_openai(
                            gemini_chunk, model, response_id
                        )
                        yield f"data: {json.dumps(openai_chunk, separators=(',',':'))}\n\n".encode()
                    except json.JSONDecodeError:
                        continue
            else:
                # 其他类型的响应，尝试直接处理
                log.warning(f"Unexpected response type: {type(gemini_response)}")
                error_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Response type error"},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()

            # 发送结束标记
            yield "data: [DONE]\n\n".encode()

        except Exception as e:
            log.error(f"Stream conversion error: {e}")
            error_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": f"Stream error: {str(e)}"},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")


# ============================================================================
# Antigravity 模型处理函数
# ============================================================================


async def handle_antigravity_request(request_data: ChatCompletionRequest):
    """
    处理 Antigravity 模型（ANT/ 前缀）的请求

    支持自动重试机制：遇到 403/401 等错误时自动切换凭证重试（最多5次）

    Args:
        request_data: ChatCompletionRequest 对象

    Returns:
        JSONResponse 或 StreamingResponse
    """
    from .antigravity_credential_manager import get_antigravity_credential_manager
    from antigravity.converter import generate_request_body
    from antigravity.client import stream_generate_content, convert_sse_to_openai_format, generate_finish_chunk
    from config import get_proxy_config, get_auto_ban_error_codes

    try:
        # 获取基础模型名（移除 ANT/ 前缀）
        base_model = get_antigravity_base_model(request_data.model)
        log.info(f"Using Antigravity model: {base_model}")

        # 获取 Antigravity 凭证管理器
        ant_cred_mgr = await get_antigravity_credential_manager()

        # 转换 OpenAI 格式到 Antigravity 格式（这部分不依赖凭证，可以提前准备）
        # 提取 system_instruction
        system_instruction = None
        user_messages = []

        for msg in request_data.messages:
            if getattr(msg, "role", None) == "system":
                # 合并所有 system 消息
                if system_instruction is None:
                    system_instruction = getattr(msg, "content", "")
                else:
                    system_instruction += "\n\n" + getattr(msg, "content", "")
            else:
                user_messages.append(msg)

        # 如果没有 system_instruction，使用默认
        if not system_instruction:
            system_instruction = "你是一个有帮助的 AI 助手。"

        # 转换消息格式
        openai_messages = []
        for msg in user_messages:
            openai_messages.append({
                "role": getattr(msg, "role", "user"),
                "content": getattr(msg, "content", "")
            })

        # 获取参数
        parameters = {
            "temperature": getattr(request_data, "temperature", 1.0),
            "max_tokens": getattr(request_data, "max_tokens", None),
            "top_p": getattr(request_data, "top_p", 0.95),
        }

        # 处理 tools
        openai_tools = None
        if hasattr(request_data, "tools") and request_data.tools:
            openai_tools = request_data.tools

        # 获取代理配置
        proxy = await get_proxy_config()

        # 处理流式响应
        is_streaming = getattr(request_data, "stream", False)

        if is_streaming:
            # 流式响应 - 带重试机制
            async def antigravity_stream_generator():
                max_retries = 5
                auto_ban_error_codes = await get_auto_ban_error_codes()

                for attempt in range(max_retries):
                    try:
                        # 获取有效凭证（每次重试都重新获取）
                        credential_result = await ant_cred_mgr.get_valid_credential(model_name=request_data.model)
                        if not credential_result:
                            error_msg = "当前无可用 Antigravity 凭证，请去控制台添加"
                            log.error(error_msg)
                            error_chunk = {
                                "id": f"chatcmpl-ant-{int(time.time() * 1000)}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": base_model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": f"Error: {error_msg}"},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
                            yield "data: [DONE]\n\n".encode()
                            return

                        account = credential_result["account"]
                        virtual_filename = credential_result["virtual_filename"]
                        access_token = account.get("access_token")

                        if not access_token:
                            log.error(f"Antigravity account {account.get('email')} missing access_token")
                            await ant_cred_mgr.force_rotate_credential()
                            continue

                        log.info(f"[Attempt {attempt + 1}/{max_retries}] Using Antigravity account: {account.get('email', 'unknown')}")

                        # 增加调用计数
                        ant_cred_mgr.increment_call_count()

                        # 生成 Antigravity 请求体
                        antigravity_payload = generate_request_body(
                            openai_messages=openai_messages,
                            model_name=base_model,
                            parameters=parameters,
                            openai_tools=openai_tools,
                            system_instruction=system_instruction
                        )

                        stream_id = f"chatcmpl-ant-{int(time.time() * 1000)}"
                        created = int(time.time())
                        has_tool_calls = False
                        success = True
                        error_code = None

                        async for chunk in stream_generate_content(antigravity_payload, access_token, proxy):
                            # 检测工具调用
                            if chunk.get('type') == 'tool_calls':
                                has_tool_calls = True

                            # 转换为 OpenAI 格式
                            openai_chunk = convert_sse_to_openai_format(chunk, base_model, stream_id, created)
                            yield openai_chunk.encode()

                        # 发送结束块
                        finish_chunk = generate_finish_chunk(base_model, has_tool_calls, stream_id, created)
                        yield finish_chunk.encode()

                        # 标记成功
                        await ant_cred_mgr.mark_credential_success(virtual_filename)

                        # 记录使用统计
                        from .antigravity_usage_stats import record_antigravity_call
                        await record_antigravity_call(virtual_filename, request_data.model)

                        # 成功返回，不再重试
                        return

                    except Exception as e:
                        error_message = str(e)
                        log.error(f"[Attempt {attempt + 1}/{max_retries}] Antigravity streaming error: {error_message}")

                        # 提取错误码（使用辅助函数）
                        error_code = _extract_error_code_from_exception(error_message)

                        # 标记凭证错误（传递完整错误消息用于 429 解析）
                        if error_code and credential_result:
                            await ant_cred_mgr.mark_credential_error(virtual_filename, error_code, error_message)

                        # 5xx 服务器错误：不切换凭证，直接等待重试
                        if error_code and 500 <= error_code < 600 and attempt < max_retries - 1:
                            base_delay = 1.5
                            delay = base_delay * (2 ** attempt)
                            log.warning(f"[RETRY] Server error {error_code}, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})")
                            await asyncio.sleep(delay)
                            continue  # 使用同一凭证重试

                        # 检查是否需要重试（使用辅助函数）
                        should_retry = await _check_should_retry_antigravity(error_code, auto_ban_error_codes)

                        if should_retry and attempt < max_retries - 1:
                            # 指数退避：base_delay * 2^attempt (1.5s, 3s, 6s, 12s...)
                            base_delay = 1.5
                            delay = base_delay * (2 ** attempt)
                            # 403/401 等错误：切换凭证并重试
                            log.warning(f"[RETRY] {error_code} error encountered, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})")
                            await ant_cred_mgr.force_rotate_credential()
                            await asyncio.sleep(delay)
                            continue
                        else:
                            # 不可重试的错误，或者重试次数用尽
                            if attempt == max_retries - 1:
                                log.error(f"Max retries ({max_retries}) exhausted for Antigravity request")

                            # 发送错误块
                            error_chunk = {
                                "id": f"chatcmpl-ant-{int(time.time() * 1000)}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": base_model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": f"Error: {error_message}"},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
                            yield "data: [DONE]\n\n".encode()
                            return

            return StreamingResponse(antigravity_stream_generator(), media_type="text/event-stream")

        else:
            # 非流式响应
            # TODO: 实现非流式响应（暂时返回错误）
            raise HTTPException(status_code=501, detail="Antigravity non-streaming mode not implemented yet")

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Antigravity request error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Antigravity request failed: {str(e)}")
