# coding: utf-8
"""
用户认证 API: 登录、登出、token 验证
"""
import time
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

from fastapi import APIRouter, HTTPException, Depends, Header, Query
from pydantic import BaseModel

from common.config import config
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix='/api/auth', tags=['auth'])

# Token 配置 (从 config.yaml 读取)
SECRET_KEY = config.get_str('auth.secret_key', 'default-secret-key-change-in-production')
TOKEN_EXPIRE_SECONDS = config.get_int('auth.token_expire_seconds', 3600)

serializer = URLSafeTimedSerializer(SECRET_KEY)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_at: str


def verify_user_password(*, user_id: object, password: str) -> bool:
    """Re-authenticate a logged-in user before a sensitive money operation."""
    if user_id is None or not password:
        return False
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            "SELECT 1 AS ok FROM mi_users WHERE id = %s AND password = %s LIMIT 1",
            (user_id, password),
        )
        return bool(cursor.fetchone())


@router.post('/login', response_model=LoginResponse)
async def login(req: LoginRequest):
    """用户登录,验证用户名密码,返回 token"""
    sql = "SELECT id, username FROM mi_users WHERE username = %s AND password = %s"
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (req.username, req.password))
            user = cursor.fetchone()
            
        if not user:
            raise HTTPException(status_code=401, detail='用户名或密码错误')
        
        # 生成 token (包含 username)
        token = serializer.dumps({'username': req.username, 'user_id': user['id']})
        expires_at = datetime.fromtimestamp(time.time() + TOKEN_EXPIRE_SECONDS)
        
        logger.info(f"用户 {req.username} 登录成功")
        return LoginResponse(
            token=token,
            username=req.username,
            expires_at=expires_at.strftime('%Y-%m-%d %H:%M:%S')
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'登录失败: {e}')
        raise HTTPException(status_code=500, detail='登录失败')


@router.post('/logout')
async def logout(authorization: str = Header(None)):
    """用户登出 (简单实现: 前端清除 token 即可)"""
    # 可选: 将 token 加入黑名单 (需要额外存储,当前简化不做)
    logger.info('用户登出')
    return {'ok': True, 'message': '已登出'}


@router.get('/verify')
async def verify_token(authorization: str = Header(None)):
    """验证 token 有效性"""
    if not authorization or not authorization.startswith('Bearer '):
        return {'valid': False, 'username': None}
    
    token = authorization.split(' ', 1)[1]
    try:
        data = serializer.loads(token, max_age=TOKEN_EXPIRE_SECONDS)
        return {'valid': True, 'username': data.get('username')}
    except (SignatureExpired, BadSignature):
        return {'valid': False, 'username': None}


def verify_token_dependency(authorization: str = Header(None)) -> dict:
    """FastAPI 依赖注入: 验证 token,无效则抛出 401"""
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='未登录')
    
    token = authorization.split(' ', 1)[1]
    try:
        data = serializer.loads(token, max_age=TOKEN_EXPIRE_SECONDS)
        return data
    except SignatureExpired:
        raise HTTPException(status_code=401, detail='登录已过期')
    except BadSignature:
        raise HTTPException(status_code=401, detail='无效的登录凭证')


def verify_ws_token(token: str) -> dict:
    """WebSocket 认证: 从 query string 验证 token"""
    if not token:
        raise HTTPException(status_code=401, detail='未提供认证凭证')
    
    try:
        data = serializer.loads(token, max_age=TOKEN_EXPIRE_SECONDS)
        return data
    except SignatureExpired:
        raise HTTPException(status_code=401, detail='登录已过期')
    except BadSignature:
        raise HTTPException(status_code=401, detail='无效的登录凭证')
