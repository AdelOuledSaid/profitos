import os
from profitos import create_app

app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5050'))
    app.run(host=os.environ.get('HOST','127.0.0.1'), port=port, debug=app.config.get('DEBUG', False))
