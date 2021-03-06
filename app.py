from hashlib import sha256
import hmac
import json
import os
import threading
import urlparse

from dropbox import Dropbox, DropboxOAuth2Flow
from dropbox.files import DeletedMetadata, FolderMetadata, WriteMode
from flask import abort, Flask, redirect, render_template, Response, request, session, url_for
from markdown import markdown
import redis

redis_url = os.environ['REDISTOGO_URL']
redis_client = redis.from_url(redis_url)

# App key and secret from the App console (dropbox.com/developers/apps)
APP_KEY = os.environ['APP_KEY']
APP_SECRET = os.environ['APP_SECRET']

app = Flask(__name__)
app.debug = True

# A random secret used by Flask to encrypt session data cookies
app.secret_key = os.environ['FLASK_SECRET_KEY']

def get_url(route):
    '''Generate a proper URL, forcing HTTPS if not running locally'''
    host = urlparse.urlparse(request.url).hostname
    url = url_for(
        route,
        _external=True,
        _scheme='http' if host in ('127.0.0.1', 'localhost') else 'https'
    )

    return url

def get_flow():
    return DropboxOAuth2Flow(
        APP_KEY,
        APP_SECRET,
        get_url('oauth_callback'),
        session,
        'dropbox-csrf-token')

@app.route('/welcome')
def welcome():
    return render_template('welcome.html', redirect_url=get_url('oauth_callback'),
        webhook_url=get_url('webhook'), home_url=get_url('index'), app_key=APP_KEY)

@app.route('/oauth_callback')
def oauth_callback():
    '''Callback function for when the user returns from OAuth.'''

    auth_result = get_flow().finish(request.args)
    account = auth_result.account_id
    access_token = auth_result.access_token

    # Extract and store the access token for this user
    redis_client.hset('tokens', account, access_token)

    process_user(account)

    return redirect(url_for('done'))

def process_user(account):
    '''Call /files/list_folder for the given user ID and process any changes.'''

    # OAuth token for the user
    token = redis_client.hget('tokens', account)

    # cursor for the user (None the first time)
    cursor = redis_client.hget('cursors', account)

    dbx = Dropbox(token)
    has_more = True

    while has_more:
        if cursor is None:
            result = dbx.files_list_folder(path='')
        else:
            result = dbx.files_list_folder_continue(cursor)

        for entry in result.entries:
            # Ignore deleted files, folders, and non-markdown files
            if (isinstance(entry, DeletedMetadata) or
                isinstance(entry, FolderMetadata) or
                not entry.path_lower.endswith('.md')):
                continue

            # Convert to Markdown and store as <basename>.html
            _, resp = dbx.files_download(entry.path_lower)
            html = markdown(unicode(resp.content, "utf-8")).encode("utf-8")
            dbx.files_upload(html, entry.path_lower[:-3] + '.html', mode=WriteMode('overwrite'))

        # Update cursor
        cursor = result.cursor
        redis_client.hset('cursors', account, cursor)

        # Repeat only if there's more to do
        has_more = result.has_more

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    return redirect(get_flow().start())

@app.route('/done')
def done():
    return render_template('done.html')

@app.route('/webhook', methods=['GET'])
def challenge():
    '''Respond to the webhook challenge (GET request) by echoing back the challenge parameter.'''

    resp = Response(request.args.get('challenge'))
    resp.headers['Content-Type'] = 'text/plain'
    resp.headers['X-Content-Type-Options'] = 'nosniff'

    return resp

@app.route('/webhook', methods=['POST'])
def webhook():
    '''Receive a list of changed user IDs from Dropbox and process each.'''

    # Make sure this is a valid request from Dropbox
    signature = request.headers.get('X-Dropbox-Signature').encode("utf-8")
    if not hmac.compare_digest(signature, hmac.new(APP_SECRET, request.data, sha256).hexdigest()):
        abort(403)

    for account in json.loads(request.data)['list_folder']['accounts']:
        # We need to respond quickly to the webhook request, so we do the
        # actual work in a separate thread. For more robustness, it's a
        # good idea to add the work to a reliable queue and process the queue
        # in a worker process.
        threading.Thread(target=process_user, args=(account,)).start()
    return ''

if __name__=='__main__':
    app.run(debug=True)
