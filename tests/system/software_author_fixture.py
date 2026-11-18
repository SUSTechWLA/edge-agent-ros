"""Deterministic model adapter for SDD-019. Real-model runs bypass design and coding replies here."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json


class Adapter(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path=="/legacy/api/chat":
            answer={"field":"unsupported","unit":"none","op":"read","threshold":0}
        elif self.path=="/design/api/chat":
            if "text-transform" not in json.loads(body["messages"][-1]["content"])["contracts"]:
                return self.send_error(400)
            answer={"interface":"text-transform","behavior":"Count whitespace-separated words equal to robot ignoring case.",
                    "inputs":"Original user text","outputs":"Integer count and bound input digest.",
                    "permissions":"Read supplied text only.","dependencies":"The installed text provider.",
                    "example_input":"ROBOT robotics robot","expected_result":"2",
                    "rollback":"Remove manifest by current digest; retain completed task history."}
        elif self.path=="/code/api/chat":
            answer={"description":"Count exact robot words ignoring case","prefix":"Count robot words",
                    "expression":"text.casefold().split().count('robot')"}
        else:
            return self.send_error(404)
        wire=json.dumps({"done":True,"message":{"content":json.dumps(answer)}}).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)


if __name__=="__main__":
    ThreadingHTTPServer(("0.0.0.0",8131),Adapter).serve_forever()
