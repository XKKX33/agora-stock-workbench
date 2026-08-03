import http.server, socketserver, os, threading, time
os.chdir(r'C:\Users\xuan\Desktop\桌面\股票\workbench\ui_mockups\v2')
h = http.server.SimpleHTTPRequestHandler
s = socketserver.TCPServer(('127.0.0.1', 8791), h)
t = threading.Thread(target=s.serve_forever, daemon=True)
t.start()
time.sleep(3600)
