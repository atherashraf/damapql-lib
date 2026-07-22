import sys

from app.damapql.dch.models import LayerAccessibility, DBConnection
from app.damapql.settings import DCH_DATABASES
from app.damapql.utils.crypto import CryptoUtils


def list_of_active_connection():
    qs = LayerAccessibility.query().all()
    con_names = []
    for obj in qs:
        # con = obj.params['conn_name']
        # con = con.split("_")
        # obj.params['conn_name'] = f"{con[0]}_{con[1]}"
        # obj.save()
        con_names.append(obj.params['conn_name'])

    con_names = set(con_names)
    for con in con_names:
        db_conn = DBConnection.query().filter(DBConnection.name == con).one()
        conn = None
        if "pak" in db_conn.params['name']:
            conn = DCH_DATABASES["pak"]
        elif "flood_forecast" in db_conn.params['name']:
            conn = DCH_DATABASES['flood_forecast']
        if conn is not None:
            crypto = CryptoUtils()
            '''
            {'engine': 'postgresql', 'host': 'localhost', 'port': 5432,
            'user': 'postgres', 'password':"", 'name': 'flood_forecast'}
            '''
            db_conn.params['host'] = conn["host"]
            db_conn.params['user'] = conn["user"]
            db_conn.params['password'] = crypto.encrypt_txt(conn["password"])
            db_conn.save()
        # print(db_conn.params)

    print(con_names)


def execute():
    params = {}
    key = None
    for val in sys.argv:
        if key is not None:
            params[key] = val
            key = None
        if "--" in val:
            key = val[2:]

    print(params)
    globals()[sys.argv[1]](**params)


if __name__ == "__main__":
    execute()
