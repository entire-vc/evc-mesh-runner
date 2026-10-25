"""Private one-shot GET worker. No retry, logging, exception text or credentials output."""
import json
import re
import sys

from .mesh import MeshClient, MeshError


def main():
    status, body = 0, None
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        path = request['path']
        if not isinstance(path, str) or not re.fullmatch(
                r'/tasks/(?:by-short-id/[0-9a-f]{8}|[0-9a-f-]{36}(?:/dependencies)?)'
                r'|/workspaces/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
                r'/mentionables\?q=(?:[A-Za-z0-9._-]|%[0-9A-Fa-f]{2}){1,80}&limit=50', path):
            raise ValueError()
        client = MeshClient(request['base'], request['key'])
        client._basic = request['basic']
        if request.get('bearer'):
            token = request['bearer']
            client._bearer = lambda: token
        body = client._bounded_get_request(path, request['timeout'])
        status = 200
    except MeshError as error:
        status = error.status
    except Exception:
        pass
    output = json.dumps({'status': status, 'body': body}).encode()
    # JSON re-encoding may expand an otherwise bounded wire response.
    if len(output) > 1048640:
        output = b'{"status":0,"body":null}'
    sys.stdout.buffer.write(output)


if __name__ == '__main__':
    main()
