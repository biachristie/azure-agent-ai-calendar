import logging
import azure.functions as func


from .function_app import main as http_handler

logging.basicConfig(level=logging.INFO)
logging.info(">>> Import OK: function_app.main carregado")


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        logging.info(">>> calendar_agent invocado")
        response = http_handler(req)

        if not isinstance(response, func.HttpResponse):
            logging.warning(">>> http_hanlder não retornou HttpResponse, convertendo para 500")
            return func.HttpResponse("Handler não retornou HttpResponse", status_code=500)

        return response
    except Exception as e:
        logging.exception(">>> EXCEÇÃO EM RUNTIME")
        return func.HttpResponse(str(e), status_code=500)
