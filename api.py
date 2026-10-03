import datetime
import hashlib
import json
import logging
import uuid
from argparse import ArgumentParser
from collections.abc import Callable
from email.message import Message
from enum import Enum
from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer,
)
from typing import Any

from scoring import get_score, get_interests


class Gender(Enum):
    UNKNOWN = 0
    MALE = 1
    FEMALE = 2


class ErrorMessage(Enum):
    BAD_REQUEST = "Bad Request"
    FORBIDDEN = "Forbidden"
    NOT_FOUND = "Not Found"
    INVALID_REQUEST = "Invalid Request"
    INTERNAL_ERROR = "Internal Server Error"


SALT = "Otus"
ADMIN_LOGIN = "admin"
ADMIN_SALT = "42"

OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
INVALID_REQUEST = 422
INTERNAL_ERROR = 500

ERRORS = {
    BAD_REQUEST: ErrorMessage.BAD_REQUEST.value,
    FORBIDDEN: ErrorMessage.FORBIDDEN.value,
    NOT_FOUND: ErrorMessage.NOT_FOUND.value,
    INVALID_REQUEST: ErrorMessage.INVALID_REQUEST.value,
    INTERNAL_ERROR: ErrorMessage.INTERNAL_ERROR.value,
}


class FieldValidationError(Exception):
    pass


class Field:

    def __init__(self, required: bool = False, nullable: bool = False):
        self.required = required
        self.nullable = nullable
        self.name: str | None = None


    def __set_name__(self, owner, name):
        self.name = name


    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.name)


    def __set__(self, instance, value):
        instance.__dict__[self.name] = value


    def validate(self, value):
        if value is None:
            if self.required:
                raise FieldValidationError(
                    f"Field '{self.name}' is required"
                )
            return

        if value == "" or value == [] or value == {}:
            if not self.nullable:
                raise FieldValidationError(
                    f"Field '{self.name}' must not be empty"
                )
            return

        self._validate_type(value)


    def _validate_type(self, value):
        pass


class CharField(Field):

    def _validate_type(self, value):
        if not isinstance(value, str):
            raise FieldValidationError(
                f"Field '{self.name}' must be a string"
            )


class ArgumentsField(Field):

    def _validate_type(self, value):
        if not isinstance(value, dict):
            raise FieldValidationError(
                f"Field '{self.name}' must be a dictionary"
            )


class EmailField(CharField):

    def _validate_type(self, value):
        super()._validate_type(value)
        if "@" not in value:
            raise FieldValidationError(
                f"Field '{self.name}' must contain '@'"
            )


class PhoneField(Field):

    def _validate_type(self, value):
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise FieldValidationError(
                f"Field '{self.name}' must be a string or integer"
            )
        s = str(value)
        if len(s) != 11 or not s.startswith("7"):
            raise FieldValidationError(
                f"Field '{self.name}' must be 11 digits starting with 7"
            )


class DateField(Field):

    def _validate_type(self, value):
        if not isinstance(value, str):
            raise FieldValidationError(
                f"Field '{self.name}' must be a string"
                f" in DD.MM.YYYY format"
            )
        try:
            datetime.datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            raise FieldValidationError(
                f"Field '{self.name}' must be a valid date"
                f" in DD.MM.YYYY format"
            )


class BirthDayField(DateField):

    def _validate_type(self, value):
        super()._validate_type(value)
        birthday = datetime.datetime.strptime(value, "%d.%m.%Y")
        now = datetime.datetime.now()
        if (now - birthday).days / 365.25 > 70:
            raise FieldValidationError(
                f"Field '{self.name}' must be within the"
                f" last 70 years"
            )


class GenderField(Field):

    def _validate_type(self, value):
        if not isinstance(value, int) or isinstance(value, bool):
            raise FieldValidationError(
                f"Field '{self.name}' must be an"
                f" integer (0, 1 or 2)"
            )
        if value not in (0, 1, 2):
            raise FieldValidationError(
                f"Field '{self.name}' must be 0, 1 or 2"
            )


class ClientIDsField(Field):

    def _validate_type(self, value):
        if not isinstance(value, list):
            raise FieldValidationError(
                f"Field '{self.name}' must be a list"
                f" of integers"
            )
        if len(value) == 0:
            raise FieldValidationError(
                f"Field '{self.name}' must not be empty"
            )
        for item in value:
            if not isinstance(item, int) or isinstance(item, bool):
                raise FieldValidationError(
                    f"Field '{self.name}' must contain"
                    f" only integers"
                )


class RequestMeta(type):

    def __new__(mcs, name, bases, namespace):
        fields: dict[str, Field] = {}
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)
        for key, value in namespace.items():
            if isinstance(value, Field):
                value.name = key
                fields[key] = value
        namespace["_fields"] = fields
        return super().__new__(mcs, name, bases, namespace)


class BaseRequest(metaclass=RequestMeta):
    _fields: dict[str, Field]

    def __init__(self, data: dict | None = None):
        data = data or {}
        for field_name in self._fields:
            setattr(self, field_name, data.get(field_name))


    def validate(self):
        errors = []
        for field_name, field in self._fields.items():
            value = getattr(self, field_name)
            try:
                field.validate(value)
            except FieldValidationError as exc:
                errors.append(str(exc))
        if errors:
            raise FieldValidationError("; ".join(errors))


    def is_valid(self):
        try:
            self.validate()
            return True, []
        except FieldValidationError as exc:
            return False, [str(exc)]


class ClientsInterestsRequest(BaseRequest):
    client_ids = ClientIDsField(required=True, nullable=False)
    date = DateField(required=False, nullable=True)


class OnlineScoreRequest(BaseRequest):
    first_name = CharField(required=False, nullable=True)
    last_name = CharField(required=False, nullable=True)
    email = EmailField(required=False, nullable=True)
    phone = PhoneField(required=False, nullable=True)
    birthday = BirthDayField(required=False, nullable=True)
    gender = GenderField(required=False, nullable=True)

    PAIRS = (
        ("phone", "email"),
        ("first_name", "last_name"),
        ("gender", "birthday"),
    )


    def validate(self):
        super().validate()
        has_pair = any(
            self._is_non_empty(getattr(self, a))
            and self._is_non_empty(getattr(self, b))
            for a, b in self.PAIRS
        )
        if not has_pair:
            raise FieldValidationError(
                "At least one pair (phone-email,"
                " first_name-last_name,"
                " gender-birthday) must have"
                " non-empty values"
            )


    @staticmethod
    def _is_non_empty(value):
        return value is not None and value != ""


    def get_non_empty_fields(self):
        return [
            name for name in self._fields
            if self._is_non_empty(getattr(self, name))
        ]


class MethodRequest(BaseRequest):
    account = CharField(required=False, nullable=True)
    login = CharField(required=True, nullable=True)
    token = CharField(required=True, nullable=True)
    arguments = ArgumentsField(required=True, nullable=True)
    method = CharField(required=True, nullable=False)

    @property
    def is_admin(self) -> bool:
        return self.login == ADMIN_LOGIN


def check_auth(request: MethodRequest) -> bool:
    if request.is_admin:
        digest = hashlib.sha512(
            (datetime.datetime.now().strftime("%Y%m%d%H")
             + ADMIN_SALT).encode("utf-8")
        ).hexdigest()
    else:
        digest = hashlib.sha512(
            (request.account + request.login
             + SALT).encode("utf-8")
        ).hexdigest()
    return digest == request.token


def handle_online_score(method_request, arguments, ctx):
    req = OnlineScoreRequest(arguments)
    valid, errors = req.is_valid()
    if not valid:
        return "; ".join(errors), INVALID_REQUEST

    ctx["has"] = req.get_non_empty_fields()

    if method_request.is_admin:
        score = 42
    else:
        score = get_score(
            phone=req.phone,
            email=req.email,
            birthday=req.birthday,
            gender=req.gender,
            first_name=req.first_name,
            last_name=req.last_name,
        )
    return {"score": score}, OK


def handle_clients_interests(method_request, arguments, ctx):
    req = ClientsInterestsRequest(arguments)
    valid, errors = req.is_valid()
    if not valid:
        return "; ".join(errors), INVALID_REQUEST

    ctx["nclients"] = len(req.client_ids)

    response = {}
    for cid in req.client_ids:
        response[str(cid)] = get_interests(str(cid))
    return response, OK


METHOD_DISPATCH = {
    "online_score": handle_online_score,
    "clients_interests": handle_clients_interests,
}


def method_handler(
    request: dict[str, Any],
    ctx: dict[str, Any],
    store: Any = None,
) -> tuple[dict[str, Any] | str, int]:
    body = request.get("body", {}) or {}

    method_request = MethodRequest(body)
    valid, errors = method_request.is_valid()
    if not valid:
        return "; ".join(errors), INVALID_REQUEST

    if not check_auth(method_request):
        return ERRORS[FORBIDDEN], FORBIDDEN

    handler_fn = METHOD_DISPATCH.get(method_request.method)
    if handler_fn is None:
        return ERRORS[NOT_FOUND], NOT_FOUND

    return handler_fn(method_request, method_request.arguments, ctx)


class MainHTTPHandler(BaseHTTPRequestHandler):
    router: dict[str, Callable] = {"method": method_handler}

    def get_request_id(self, headers: Message) -> str:
        return headers.get(
            "HTTP_X_REQUEST_ID", uuid.uuid4().hex
        )


    def do_POST(self):
        response, code = {}, OK
        context = {
            "request_id": self.get_request_id(self.headers)
        }
        request = None
        try:
            data_string = self.rfile.read(
                int(self.headers["Content-Length"])
            )
            request = json.loads(data_string)
        except Exception:
            code = BAD_REQUEST

        if request:
            path = self.path.strip("/")
            logging.info(
                "%s: %s %s" % (self.path, data_string,
                                context["request_id"]))
            if path in self.router:
                try:
                    response, code = self.router[path](
                        {"body": request,
                         "headers": self.headers},
                        context,
                        None,
                    )
                except Exception as e:
                    logging.exception(
                        "Unexpected error: %s" % e
                    )
                    code = INTERNAL_ERROR
            else:
                code = NOT_FOUND

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if code not in ERRORS:
            r = {"response": response, "code": code}
        else:
            r = {"error": response or ERRORS.get(
                     code, "Unknown Error"),
                 "code": code}
        context.update(r)
        logging.info(context)
        self.wfile.write(json.dumps(r).encode("utf-8"))


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "-p", "--port", action="store",
        type=int, default=8080
    )
    parser.add_argument(
        "-l", "--log", action="store", default=None
    )
    args = parser.parse_args()

    logging.basicConfig(
        filename=args.log,
        level=logging.INFO,
        format="[%(asctime)s] %(levelname).1s %(message)s",
        datefmt="%Y.%m.%d %H:%M:%S",
    )

    server = HTTPServer(
        ("localhost", args.port), MainHTTPHandler
    )

    logging.info("Starting server at %s" % args.port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    server.server_close()
