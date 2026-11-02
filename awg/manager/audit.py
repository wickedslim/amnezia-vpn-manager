"""Structured metadata only: never arguments, exception text, keys or credentials."""
import asyncio
import functools
import inspect
import logging
import sqlite3
import time
import uuid
from contextvars import ContextVar

context=ContextVar('audit_context',default=None)

def begin(uid,source):
    return context.set(dict(actor=uid,source=source,request_id=uuid.uuid4().hex[:16]))

def record(store,action,result,sid=None,cid=None,error=None,elapsed=0):
    ctx=context.get() or dict(actor=None,source='system',request_id=uuid.uuid4().hex[:16])
    try:
        store.audit_record(**ctx,action=action,result=result,sid=sid,cid=cid,error=error,elapsed=elapsed)
    except sqlite3.Error as exc:
        logging.getLogger(__name__).error('Audit persistence failed: request=%s action=%s type=%s',ctx['request_id'],action,type(exc).__name__)

def audited(action):
    def decorate(fn):
        signature=inspect.signature(fn)
        @functools.wraps(fn)
        async def wrapped(self,*args,**kwargs):
            params=signature.bind(self,*args,**kwargs).arguments
            sid=params.get('sid',getattr(params.get('server'),'id',None));cid=params.get('cid');started=time.monotonic()
            try:
                value=await fn(self,*args,**kwargs)
            except BaseException as exc:
                record(self.store,action,'unknown' if isinstance(exc,asyncio.CancelledError) else 'failed',sid,cid,type(exc).__name__,int((time.monotonic()-started)*1000))
                raise
            record(self.store,action,'ok',sid,cid,elapsed=int((time.monotonic()-started)*1000))
            return value
        return wrapped
    return decorate
