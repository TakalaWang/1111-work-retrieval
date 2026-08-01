from work_retrieval_api.app import create_app
from work_retrieval_api.runtime import runtime_from_environment

app = create_app(runtime_from_environment)
