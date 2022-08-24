from __future__ import annotations
import os
import sys
import json
import time
import asyncio
import contextlib
from typing import List
import requests
from http.cookiejar import LWPCookieJar
from urllib.parse import urlencode
from . import util
from .util import color
import tqdm

log = util.getLogger(__name__, level='info')


# URL = 'https://eng-nrf233-01.engineering.nyu.edu/ptg/api'
URL = 'https://api.ptg.poly.edu'
LOCAL_URL = 'http://localhost:7890'




class WebsocketStream:
    '''Encapsulates a websocket stream to read/write in the format that the server sends it (json offsets, bytes, json, bytes, etc.)'''
    def __init__(self, *a, params=None, **kw):
        self.a, self.kw = a, kw
        self.params = params or {}
        self.kw.setdefault('close_timeout', 10)

    async def __await__(self):
        await asyncio.sleep(1e-6)
        return await self.connect

    async def __aenter__(self):
        import websockets
        self.connect = websockets.connect(*self.a, **self.kw)
        self.ws = await self.connect.__aenter__()
        return self
    
    async def __aexit__(self, c, e, tb):
        from websockets.exceptions import ConnectionClosed
        e = await self.connect.__aexit__(c, e, tb)
        del self.connect, self.ws
        return c and issubclass(c, ConnectionClosed)

    

class DataStream(WebsocketStream):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.batch = self.params.get('batch')
        self.ack = self.params.get('ack')

    async def recv_data(self):
        await asyncio.sleep(1e-6)
        offsets = json.loads(await self.ws.recv())
        content = await self.ws.recv()
        return util.unpack_entries(offsets, content)

    async def send_data(self, data):
        offsets, entries = util.pack_entries([data] if not self.batch else data)
        if self.batch:
            await self.ws.send(','.join(map(str, offsets)))
        await self.ws.send(bytes(entries))
        if self.ack:
            await self.ws.recv()  # ack
        await asyncio.sleep(1e-6)


class ReplayStream(WebsocketStream):
    def __init__(self, *a, show_pbar=True, **kw):
        super().__init__(*a, **kw)
        self.show_pbar = show_pbar
    
    async def __aenter__(self):
        self.pbars = {}
        self.ts_se = {}
        return await super().__aenter__()

    async def __aexit__(self, *a):
        for p in self.pbars.values():
            p.close()
        return await super().__aexit__(*a)

    async def progress(self):
        progress = json.loads(await self.ws.recv())
        if self.show_pbar:
            for sid, n in progress['updates'].items():
                if sid not in self.pbars:
                    self.pbars[sid] = tqdm.tqdm(desc=sid, total=int(progress['durations'][sid]))
                # self.pbars[sid].update(n)
                self.pbars[sid].n = int(progress['current'][sid])
                self.pbars[sid].refresh()
        return progress['active']

    async def done(self):
        while await self.progress():
            pass



class API:
    '''The PTG API. 
    
    This let's you operate PTG from Python or CLI.
    '''
    _TOKEN_CACHE = '~/.ptg'
    _COOKIE_FILE = '~/.ptg.cjar'
    _token = None

    def __init__(self, url: str|None=None, token=None, username: str|None=None, password: str|None=None, local: bool=False):
        url = url or (LOCAL_URL if local else URL)
        # get url and make sure that it has a protocol on it.
        self.url = url = url.rstrip('/')
        secure = True if url.startswith('https://') else False if url.startswith('http://') else None
        uri = url.split('://', 1)[1] if secure is not None else url

        # get websocket url
        self._wsurl = f"ws{'s' if secure else ''}://{uri}"

        self.sess = requests.Session()
        cookies = LWPCookieJar(os.path.expanduser(self._COOKIE_FILE))
        self.sess.cookies: LWPCookieJar = cookies  # type: ignore
        try:
            cookies.load()
        except Exception:
            pass
        self.token = util.Token.from_cookiejar(self.sess.cookies, 'authorization')

        # check token login
        # self._TOKEN_CACHE = token_cache = os.path.expanduser(self._TOKEN_CACHE) if self._TOKEN_CACHE else None
        if token:
            self.token = token
        elif username or password:
            self.login(username, password)
        # elif token_cache and os.path.isfile(token_cache):
        #     with open(token_cache, 'r') as f:
        #         self.token = f.read().strip()
        # elif TOKEN:
        #     self.token = util.Token(TOKEN)
        # elif ask_login:
        #     raise NotImplementedError
        # elif require_login:
        #     raise ValueError("Could not log in.")

    @property
    def token(self) -> util.Token | None:
        '''The user JWT token used for making authenticated requests.'''
        return self._token

    @token.setter
    def token(self, token):
        self._token = util.Token(token)
    

    def login(self, username: str, password: str):
        '''Login using a username and password to retrieve a token.'''
        assert username and password, "I assume you don't want an empty username and password"
        # get token
        log.info('login: %s %s', username, f'{self.url}/token')
        r = self.sess.post(url=f'{self.url}/token', data={'username': username, 'password': password})
        # # store token
        # self.token = r.json()['access_token']
        # if self._TOKEN_CACHE:
        #     with open(self._TOKEN_CACHE, 'w') as f:
        #         f.write(str(self.token))
        self.sess.cookies.save()
        self.token = util.Token.from_cookiejar(self.sess.cookies, 'authorization')

    def logout(self):
        '''Logout and discard the token.'''
        self.token = None
        if self._TOKEN_CACHE and os.path.isfile(self._TOKEN_CACHE):
            os.remove(self._TOKEN_CACHE)

    # make a request

    def _headers(self, headers: dict|None=None, **kw) -> dict:
        return {
            'Authorization': f'Bearer {self.token}' if self.token else None, 
            **(headers or {}), **kw}

    def _do(self, method: str, *url_parts, headers: dict|None=None, params: dict|None=None, raises: bool=True, **kw) -> requests.Response:
        '''Generic request wrapper.'''
        url = '/'.join((self.url, *(str(u) for u in url_parts if u is not None)))
        # headers = self._headers(headers)
        if params:
            kw['params'] = {k: v for k, v in params.items() if k and v is not None}

        # make the request and time it
        log.info('request: %s %s', method, url)
        log.debug('headers: %s', headers)
        log.debug('request args: %s', kw)
        t0 = time.time()
        r = self.sess.request(method, url, headers=headers, **kw)
        log.info('%d took %.3g secs', r.status_code, time.time() - t0)
        if raises:
            r.raise_for_status()
        return r

    def _get(self, *a, **kw) -> requests.Response: return self._do('GET', *a, **kw)
    def _put(self, *a, **kw) -> requests.Response: return self._do('PUT', *a, **kw)
    def _post(self, *a, **kw) -> requests.Response: return self._do('POST', *a, **kw)
    def _delete(self, *a, **kw) -> requests.Response: return self._do('DELETE', *a, **kw)

    def _ws(self, *url_parts, headers: dict|None=None, connect_kwargs: dict|None=None, cls=WebsocketStream, **params):
        # import websockets
        params_str = urlencode({k: v for k, v in params.items() if k and v is not None}) if params else ''
        headers = self._headers(headers)
        url = os.path.join(self._wsurl, *map(str, url_parts)) + (f'?{params_str}' if params_str else '')
        log.info('websocket connect: %s', url)
        # return websockets.connect(url, extra_headers=headers, **(connect_kwargs or {}))
        return cls(url, params=params, extra_headers=headers, **(connect_kwargs or {}))

    def ping(self, error=False):
        if error:
            return self._get('ping/error').json()
        return self._get('ping').json()


    # manage streams

    class streams(util.Nest):
        '''Data Stream metadata.'''
        def ls(self, info: bool|None=None) -> list:
            '''Get all streams'''
            return self._get('streams/', params={'info': info}).json()

        def ls2(self) -> dict:
            '''Get all streams'''
            return {
                s: self.get(s)
                for s in self.ls()
            }

        def get(self, id: str, report_error: bool|None=None) -> dict:
            '''Get a stream.
            
            Arguments:
                id (str): The stream ID.
            '''
            return self._get('streams', id, params={'report_error': report_error}).json()

        def update(self, id: str, **meta):
            '''Update a stream's metadata.
            
            Arguments:
                id (str): The stream ID.
                desc (str): The stream description.
                override (bool): Whether to overwrite an existing stream. Otherwise it will throw an error.
                **meta: Any arbitrary metadata to attach to the stream.
            '''
            return self._put('streams', id, json=meta).json()

        def delete(self, id: str) -> bool:
            '''Delete a stream.
            
            Arguments:
                id (str): The stream ID.
            '''
            return self._delete('streams', id).json()


    # recordings

    class recordings(util.Nest):
        '''Data Stream metadata.'''
        def ls(self, info: bool|None=None) -> list:
            '''Get all recordings'''
            return self._get('recordings', params={'info': info}).json()

        def current(self, info: bool|None=None) -> list:
            '''Get the current recording'''
            return self._get('recordings/current', params={'info': info}).json()

        def get(self, id: str) -> dict:
            '''Get a recording.
            
            Arguments:
                id (str): The stream ID.
            '''
            return self._get('recordings', id).json()

        def start(self, *id):
            '''Start a recording.
            
            Arguments:
                id (str): The recording ID.
            '''
            return self._put('recordings/start', params={'rec_id': '-'.join(id).replace(' ', '-') or None}).json()

        def stop(self):
            '''Stop a recording.
            '''
            return self._put('recordings/stop').json()

        def rename(self, id: str, new_id: str) -> bool:
            '''Rename a recording.
            
            Arguments:
                id (str): The recording ID.
                new_id (str): The new recording ID.
            '''
            return self._put('recordings', id, 'rename', new_id).json()

        def delete(self, id: str) -> bool:
            '''Delete a recording.
            
            Arguments:
                id (str): The recording ID.
            '''
            return self._delete('recordings', id).json()

        def static(self, *fs, out_dir='.', display=False):
            if not any(fs):
                raise ValueError('You must provide a link to a static file')
            r = self._get('recordings/static', *fs)
            if display:
                print(r.content)
                return

            fname = os.path.join(out_dir, '-'.join(map(str, fs)))
            download_file(r, fname)
            print('wrote to', fname)

        # def raw_static(self, *fs, out_dir='.', display=False):
        #     if not any(fs):
        #         raise ValueError('You must provide a link to a static file')
        #     r = self._get('recordings/static/raw', *fs)
        #     if display:
        #         print(r.content)
        #         return

        #     fname = os.path.join(out_dir, '-'.join(map(str, fs)))
        #     download_file(r, fname)
        #     print('wrote to', fname)

        def upload(self, recording_id, fname, path, overwrite=None):
            '''Upload a file to an existing recording.
            
            Arguments:
                recording_id (str): The recording ID to save to
                fname (str): The name to give the file on the server (including proper extension)
                path (str): The path to the file locally.
                overwrite (bool): By default, if a file exists it will throw an error.
                    Use this to force overwrite an existing file. Be careful not to overwrite the original streams.
            '''
            r = self._post(
                'recordings/upload', recording_id, fname, 
                params={'overwrite': overwrite},
                files={'file': open(path, 'rb')})

        def replay_connect(self, rec_id, stream_ids, prefix=None, fullspeed=None, interval=None):
            '''Replay a recording

            Arguments:
                rec_id (str): The recording ID
                stream_ids (str): The ID(s) of the streams to be replayed (separated by '+')
                prefix (str): A prefix to be added to the replayed Redis Stream (e.g. 'replay:')
                fullspeed (bool): set to true to ignore the timestamps and play the data as fast as possible (default: False)
                interval (float): specify how often the progress to be updated (default: 1 second)

            .. code-block: shell
            ptgctl recordings replay coffee-test-1 main+depthlt --prefix "replay:"

            '''
            return self._ws(
                'recordings/replay', 
                cls=ReplayStream,
                rec_id=rec_id, 
                prefix=prefix, 
                sid=stream_ids, 
                fullspeed=fullspeed, 
                interval=interval)

        @util.async2sync
        async def replay(self, rec_id, stream_ids, prefix='', fullspeed=False, interval=1):
            '''Replay a recording

            Arguments:
                rec_id (str): The recording ID
                stream_ids (str): The ID(s) of the streams to be replayed (separated by '+')
                prefix (str): A prefix to be added to the replayed Redis Stream (e.g. 'replay:')
                fullspeed (bool): set to true to ignore the timestamps and play the data as fast as possible (default: False)
                interval (float): specify how often the progress to be updated (default: 1 second)

            .. code-block: shell
            ptgctl recordings replay coffee-test-1 main+depthlt --prefix "replay:"

            '''
            async with self.replay_connect(rec_id, stream_ids, prefix, fullspeed, interval) as c:
                while await c.progress():
                    pass

        replay_async = replay.asyncio

        # def replay(self, rec_id, stream_ids, prefix='', fullspeed=False, interval=1):
        #     import asyncio
        #     return asyncio.run(self.replay_async(
        #         rec_id, stream_ids, 
        #         prefix=prefix, fullspeed=fullspeed, interval=interval))

    # recipes

    class recipes(util.Nest):
        '''Manage recipes.'''
        def ls(self) -> list:
            '''Get all recipes.'''
            return self._get('recipes').json()

        def get(self, id: str) -> dict:
            '''Get a recipe by ID.
            
            Arguments:
                id (str): The recipe ID.
            '''
            return self._get('recipes', id).json()

        def new(self, recipe):
            '''Create a recipe.
            
            Arguments:
                recipe (dict): The recipe info. Can also be a path to a JSON file.
                    name (str): The human-readable name of the recipe
                    ingredients (list[str]): The recipe ingredients
                    tools (list[str]): The recipe tools
                    instructions (list[str]): The recipe instruction steps.
            '''
            if isinstance(recipe, str):
                if os.path.isfile(recipe):
                    recipe = open(recipe, 'r').read()
                recipe = json.loads(recipe)
            return self._post('recipes', json=recipe).json()

        def update(self, id: str, recipe: dict|str) -> bool:
            '''Update a recipe.
            
            Arguments:
                id (str): The recipe ID. Can also be a path to a JSON file.
                recipe (dict): The recipe info. 
                    name (str): The human-readable name of the recipe
                    ingredients (list[str]): The recipe ingredients
                    tools (list[str]): The recipe tools
                    instructions (list[str]): The recipe instruction steps.
            '''
            if isinstance(recipe, str):
                if os.path.isfile(recipe):
                    recipe = open(recipe, 'r').read()
                recipe = json.loads(recipe)
            return self._put('recipes', id, json=recipe).json()

        def delete(self, id: str) -> bool:
            '''Delete a recipe.
            
            Arguments:
                id (str): The recipe ID.
            '''
            return self._delete('recipes', id).json()


    # session

    class sessions(util.Nest):
        '''Session management.'''
        def get(self) -> dict:
            return self._get('sessions').json()

        def current_recipe(self, info=None) -> str|dict:
            return self._get('sessions', 'recipe', params={'info': info}).json()

        def start_recipe(self, recipe_id: str) -> List[bool]:  # id was set, step was set
            return self._put('sessions', 'recipe', recipe_id).json()

        def stop_recipe(self) -> bool:
            return self._delete('sessions', 'recipe').json()

        def get_recipe_step(self) -> int:
            return self._get('sessions', 'recipe/step').json()

        def set_recipe_step(self, step_id: int) -> bool:
            return self._put('sessions', 'recipe/step', step_id).json()

        # def ls(self) -> list:
        #     '''Get all sessions.'''
        #     return self._get('sessions').json()

        # def get(self, id: str) -> dict:
        #     '''Get a session by ID.
            
        #     Arguments:
        #         id (str): The session ID.
        #     '''
        #     return self._get('sessions', id).json()

        # def new(self, recipe_id: str, step_index: int=None):
        #     '''Create a session.
            
        #     Arguments:
        #         id (str): The session ID.
        #     '''
        #     return self._post('sessions', json=util.filternone({
        #         'recipe_id': recipe_id,
        #         'step_index': step_index,
        #     })).json()

        # def update(self, id, recipe_id=None, step_index=None) -> bool:
        #     '''Update a session.
            
        #     Arguments:
        #         id (str): The session ID.
        #     '''
        #     return self._put('sessions', id, json=util.filternone({
        #         'recipe_id': recipe_id,
        #         'step_index': step_index,
        #     })).json()

        # def delete(self, id: str) -> bool:
        #     '''Delete a session.
            
        #     Arguments:
        #         id (str): The session ID.
        #     '''
        #     return self._delete('sessions', id).json()

        # def step(self, id: str) -> int:
        #     '''Get the current step.'''
        #     return self._get('sessions', id, 'step').json()

        # def update_step(self, id: str, step: int=None) -> bool:
        #     '''Set the current step.'''
        #     return self._put('sessions', id, 'step', step).json()


    # data

    def data(self, stream_id: str, **kw) -> dict:
        '''Get the latest data
        
        Arguments:
            id (str): The stream ID.
            **params: url query params for request.
        '''
        r = self._get('data', stream_id, params=kw)
        return util.unpack_entries(json.loads(r.headers['entry-offset']), r.content)

    def upload_data(self, stream_id: str, data: dict, **kw) -> dict:
        '''Upload a data packet. Not Implemented.
        
        Arguments:
            id (str): The stream ID.
            **params: url query params for request.
        '''
        if not isinstance(data, (list, tuple)):
            data = [data]
        
        if isinstance(stream_id, (list, tuple)):
            stream_ids = stream_id
            stream_id = '*'
        else:
            stream_ids = [stream_id] * len(data)
        return self._post('data', stream_id, params=kw, files=dict(zip(stream_ids, data))).json()

    # data over async websockets

    def data_pull_connect(self, stream_id: str, **kw) -> 'WebsocketStream':
        '''Connect to the server and send data over an asynchronous websocket.
        
        .. code-block:: python

            async with api.data_pull_connect(stream_id, **kw) as ws:
                while True:
                    for sid, ts, data in await ws.aread():
                        img = np.array(Image.open(io.BytesIO(data)))  # rgb array
        '''
        return self._ws('data', stream_id, 'pull', cls=DataStream, **kw)

    def data_push_connect(self, stream_id: str, **kw) -> 'WebsocketStream':
        '''Connect to the server and send data over an asynchronous websocket.
        
        .. code-block:: python

            async with api.data_push_connect(stream_id, **kw) as ws:
                while True:
                    img = get_some_image()

                    # write image to jpg
                    output = io.BytesIO()
                    pil.fromarray(img).save(output, format='jpeg')
                    data = output.getvalue()

                    # TODO: support timestamps + multiple sid
                    await ws.awrite(data)
        '''
        return self._ws('data', stream_id, 'push', cls=DataStream, **kw)

    # tools

    @util.bound_module
    def display(self) -> util.BoundModule:  # lazy import and bind
        from .tools import display
        return display

    # @util.bound_module
    # def mock(self) -> util.BoundModule:  # lazy import and bind
    #     from .tools import mock
    #     return mock

    @util.bound_module
    def test(self) -> util.BoundModule:  # lazy import and bind
        from .tools import test
        return test

    # @util.bound_module
    # def local_record(self) -> util.BoundModule:  # lazy import and bind
    #     from .tools import local_record
    #     return local_record


UNIT=1024**2
def download_file(r, fname=None, block_size=1024):
    import tqdm
    total_size= int(r.headers.get('content-length', 0)) / UNIT
    with tqdm.tqdm(total=total_size, desc=f'writing to {fname}', unit='mb', unit_scale=True, leave=False) as pbar:
        with open(fname, 'wb') as f:
            for data in r.iter_content(block_size):
                pbar.update(len(data)/UNIT)
                f.write(data)


# This just implements a few command line features that we may not necessarily want from the python side
class CLI(API):
#     '''

#       ___           ___           ___           ___           ___           ___   
#      /\  \         /\  \         /\  \         /\  \         /\  \         /\__\  
#     /::\  \        \:\  \       /::\  \       /::\  \        \:\  \       /:/  /  
#    /:/\:\  \        \:\  \     /:/\:\  \     /:/\:\  \        \:\  \     /:/  /   
#   /::\~\:\  \       /::\  \   /:/  \:\  \   /:/  \:\  \       /::\  \   /:/  /    
#  /:/\:\ \:\__\     /:/\:\__\ /:/__/_\:\__\ /:/__/ \:\__\     /:/\:\__\ /:/__/     
#  \/__\:\/:/  /    /:/  \/__/ \:\  /\ \/__/ \:\  \  \/__/    /:/  \/__/ \:\  \     
#       \::/  /    /:/  /       \:\ \:\__\    \:\  \         /:/  /       \:\  \    
#        \/__/     \/__/         \:\/:/  /     \:\  \        \/__/         \:\  \   
#                                 \::/  /       \:\__\                      \:\__\  
#                                  \/__/         \/__/                       \/__/  

#     '''
    def _do(self, *a, **kw):
        # make sure we're logged in. If not, prompt
        if not self.token:
            self.login()
        return super()._do(*a, **kw)

    def login(self, username=None, password=None):
        # prompt for missing username/password
        if not username or not password:
            log.warning("You're not logged in:")
            if not username:
                username = input(color("What is your username? ", 'purple', 1))
            if not password:
                import getpass
                password = getpass.getpass(color("What is your password? ", 'purple', 1))
        return super().login(username, password)




def main():
    '''Create an auto-magical CLI !! really that's it.'''
    from .util.cli_format import yamltable, indent  # installs a formatter for fire
    import fire
    try:
        fire.Fire(CLI)
    except requests.HTTPError as e:
        r = e.response
        if 'application/json' in r.headers.get('Content-Type'):
            content = r.json()
            content = yamltable(content)
        else:
            content = r.content
        # content = r.json() if 'application/json' in r.headers.get('Content-Type') else r.content
        log.error('Error: %d\nContent: \n%s', r.status_code, color(indent(f'{content}', 2), 'red', 1))
        sys.exit(1)



if __name__ == '__main__':
    main()
