"""Owner-managed Telegram administrators. No username-based authentication."""
from .domain import ManagerError


class Access:
    def __init__(self,store,owner):self.store,self.owner=store,owner
    def admins(self):return self.store.get_pref('telegram_admins',[]) or []
    def allowed(self,uid):return type(uid) is int and (uid==self.owner or any(a['id']==uid for a in self.admins()))
    def require_owner(self,uid):
        if uid!=self.owner:raise ManagerError('Это действие доступно только владельцу бота.')
    def grant(self,actor,uid,name):
        self.require_owner(actor)
        if type(uid) is not int or not 0<uid<2**52 or uid==self.owner:
            raise ManagerError('Укажите числовой Telegram ID другого пользователя.')
        if not isinstance(name,str) or not name.strip() or len(name.strip())>64:
            raise ManagerError('Укажите имя администратора до 64 символов.')
        rows=self.admins()
        if any(a['id']==uid for a in rows):raise ManagerError('Этот администратор уже добавлен.')
        if len(rows)>=20:raise ManagerError('Достигнут лимит: 20 администраторов.')
        self.store.set_pref('telegram_admins',rows+[{'id':uid,'name':name.strip()}])
        from .audit import record
        record(self.store,f'admin.grant:{uid}','ok')
    def revoke(self,actor,uid):
        self.require_owner(actor)
        if uid==self.owner:raise ManagerError('Нельзя удалить владельца бота.')
        self.store.set_pref('telegram_admins',[a for a in self.admins() if a['id']!=uid])
        self.store.clear_wizard(uid)
        from .audit import record
        record(self.store,f'admin.revoke:{uid}','ok')
