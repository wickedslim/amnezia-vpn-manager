"""Owner-managed Telegram administrators. No username-based authentication."""
from .domain import ManagerError


class Access:
    def __init__(self,store,owner):self.store,self.owner=store,owner
    def admins(self):return self.store.get_pref('telegram_admins',[]) or []
    def allowed(self,uid):return type(uid) is int and (uid==self.owner or any(a['id']==uid for a in self.admins()))
    def permits(self, uid, sid, operation=None):
        if uid == self.owner:
            return True
        for admin in self.admins():
            if admin['id'] == uid:
                servers = admin.get('servers')
                operations = admin.get('operations')
                return ((servers is None or sid in servers)
                        and (operation is None or operations is None or operation in operations))
        return False

    def set_scope(self, actor, uid, servers, operations):
        self.require_owner(actor)
        if servers is not None:
            known = {server.id for server in self.store.servers()}
            if not isinstance(servers, list) or any(type(sid) is not int or sid not in known for sid in servers):
                raise ManagerError('Выберите доступные серверы.')
        if not isinstance(operations, list) or any(op not in ('delete', 'reissue', 'reboot', 'pause') for op in operations):
            raise ManagerError('Проверьте права администратора.')
        rows = self.admins()
        for admin in rows:
            if admin['id'] == uid:
                admin.update(servers=servers, operations=list(dict.fromkeys(operations)))
                self.store.set_pref('telegram_admins', rows)
                from .audit import record
                record(self.store, f'admin.scope:{uid}', 'ok')
                return
        raise ManagerError('Администратор не найден.')

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
