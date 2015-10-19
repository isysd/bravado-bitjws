# -*- coding: utf-8 -*-
"""
The :class:`SwaggerClient` provides an interface for making API calls based on
a swagger spec, and returns responses of python objects which build from the
API response.

Structure Diagram::

        +---------------------+
        |                     |
        |    SwaggerClient    |
        |                     |
        +------+--------------+
               |
               |  has many
               |
        +------v--------------+
        |                     |
        |     Resource        +------------------+
        |                     |                  |
        +------+--------------+         has many |
               |                                 |
               |  has many                       |
               |                                 |
        +------v--------------+           +------v--------------+
        |                     |           |                     |
        |     Operation       |           |    SwaggerModel     |
        |                     |           |                     |
        +------+--------------+           +---------------------+
               |
               |  uses
               |
        +------v--------------+
        |                     |
        |     HttpClient      |
        |                     |
        +---------------------+


To get a client

.. code-block:: python

    client = bravado.client.SwaggerClient.from_url(swagger_spec_url)
"""
import functools
import logging
import sys
import bitjws

from bravado_core.docstring import create_operation_docstring
from bravado_core.exception import MatchingResponseNotFound
from bravado_core.exception import SwaggerMappingError
from bravado_core.param import marshal_param
from bravado_core.response import unmarshal_response
from bravado_core.spec import Spec
import six
from six import iteritems, itervalues
from six.moves.urllib import parse as urlparse

from bravado.docstring_property import docstring_property
from bravado.exception import HTTPError
from bravado.requests_client import RequestsClient
from bravado.swagger_model import Loader
from bravado.warning import warn_for_deprecated_op

from bravado.client import *
from bravado_bitjws.requests_client import BitJWSRequestsClient

log = logging.getLogger(__name__)


class BitJWSSwaggerClient(SwaggerClient):
    """
    A client for accessing a Swagger-documented RESTful service,
    which also uses bitjws authentication.
    """

    def __init__(self, swagger_spec, resource_decorator=None):
        """
        :param swagger_spec: :class:`bravado_core.spec.Spec`
        :param resource_decorator: The ResourceDecorator class to use
        :type  resource_decorator: ResourceDecorator
        """
        super(BitJWSSwaggerClient, self).__init__(swagger_spec,
                resource_decorator=resource_decorator)

    @classmethod
    def from_url(cls, spec_url, http_client=None, request_headers=None,
                 config=None, resource_decorator=None, privkey=None):
        """
        Build a :class:`SwaggerClient` from a url to the Swagger
        specification for a RESTful API.

        :param spec_url: url pointing at the swagger API specification
        :type spec_url: str
        :param http_client: an HTTP client used to perform requests
        :type  http_client: :class:`bravado.http_client.HttpClient`
        :param request_headers: Headers to pass with http requests
        :type  request_headers: dict
        :param config: bravado_core config dict. See
            bravado_core.spec.CONFIG_DEFAULTS
        :param resource_decorator: The ResourceDecorator class to use
        :type  resource_decorator: ResourceDecorator
        """
        log.debug(u"Loading from %s" % spec_url)
        if privkey is None:
            privkey = bitjws.PrivateKey()
        elif isinstance(privkey, str):
            privkey = bitjws.PrivateKey(bitjws.wif_to_privkey(privkey))

        if http_client is None:
            host = urlparse.urlsplit(spec_url).hostname
            http_client = BitJWSRequestsClient()
            http_client.set_bitjws_key(host,
                    bitjws.privkey_to_wif(privkey.private_key))
        loader = Loader(http_client, request_headers=request_headers)
        spec_dict = loader.load_spec(spec_url)
        return cls.from_spec(spec_dict, spec_url, http_client, config,
                             resource_decorator, privkey)

    @classmethod
    def from_spec(cls, spec_dict, origin_url=None, http_client=None,
                  config=None, resource_decorator=None, privkey=None):
        """
        Build a :class:`SwaggerClient` from swagger api docs

        :param spec_dict: a dict with a Swagger spec in json-like form
        :param origin_url: the url used to retrieve the spec_dict
        :type  origin_url: str
        :param config: Configuration dict - see spec.CONFIG_DEFAULTS
        :param resource_decorator: The ResourceDecorator class to use
        :type  resource_decorator: ResourceDecorator
        :param str privkey: The WIF private key to use for bitjws signing
        """
        if privkey is None:
            privkey = bitjws.PrivateKey()
        elif isinstance(privkey, str):
            privkey = bitjws.PrivateKey(bitjws.wif_to_privkey(privkey))

        if http_client is None:
            host = urlparse.urlsplit(origin_url).hostname
            http_client = BitJWSRequestsClient()
            http_client.set_bitjws_key(host,
                    bitjws.privkey_to_wif(privkey.private_key))

        resource_decorator = resource_decorator or BitJWSResourceDecorator
        swagger_spec = Spec.from_dict(
            spec_dict, origin_url, http_client, config)
        return cls(swagger_spec, resource_decorator)


class BitJWSResourceDecorator(object):
    """
    Wraps :class:`bravado_core.resource.Resource` so that accesses to contained
    operations can be instrumented.
    """

    def __init__(self, resource):
        """
        :type resource: :class:`bravado_core.resource.Resource`
        """
        self.resource = resource

    def __getattr__(self, name):
        """
        :rtype: :class:`CallableOperation`
        """
        return BitJWSCallableOperation(getattr(self.resource, name))

    def __dir__(self):
        """
        Exposes correct attrs on resource when tab completing in a REPL
        """
        return self.resource.__dir__()


class BitJWSCallableOperation(object):
    """
    Wraps an operation to make it callable and provide a docstring. Calling
    the operation uses the configured http_client.
    """
    def __init__(self, operation):
        """
        :type operation: :class:`bravado_core.operation.Operation`
        """
        self.operation = operation

    @docstring_property(__doc__)
    def __doc__(self):
        return create_operation_docstring(self.operation)

    def __getattr__(self, name):
        """
        Forward requests for attrs not found on this decorator to the delegate.
        """
        return getattr(self.operation, name)

    def construct_request(self, **op_kwargs):
        """
        :param op_kwargs: parameter name/value pairs to passed to the
            invocation of the operation.
        :return: request in dict form
        """
        request_options = op_kwargs.pop('_request_options', {})
        url = self.operation.swagger_spec.api_url.rstrip('/') + self.path_name
        request = {
            'method': self.operation.http_method.upper(),
            'url': url,
            'params': {},  # filled in downstream
            'headers': request_options.get('headers', {}),
        }

        # Copy over optional request options
        for request_option in ('connect_timeout', 'timeout'):
            if request_option in request_options:
                request[request_option] = request_options[request_option]

        self.construct_params(request, op_kwargs)
        return request

    def construct_params(self, request, op_kwargs):
        """
        Given the parameters passed to the operation invocation, validates and
        marshals the parameters into the provided request dict.

        :type request: dict
        :param op_kwargs: the kwargs passed to the operation invocation
        :raises: SwaggerMappingError on extra parameters or when a required
            parameter is not supplied.
        """
        current_params = self.operation.params.copy()
        for param_name, param_value in iteritems(op_kwargs):
            param = current_params.pop(param_name, None)
            if param is None:
                raise SwaggerMappingError(
                    "{0} does not have parameter {1}"
                    .format(self.operation.operation_id, param_name))
            marshal_param(param, param_value, request)

        # Check required params and non-required params with a 'default' value
        for remaining_param in itervalues(current_params):
            if remaining_param.required:
                raise SwaggerMappingError(
                    '{0} is a required parameter'.format(remaining_param.name))
            if not remaining_param.required and remaining_param.has_default():
                marshal_param(remaining_param, None, request)

    def __call__(self, **op_kwargs):
        """
        Invoke the actual HTTP request and return a future that encapsulates
        the HTTP response.

        :rtype: :class:`bravado.http_future.HTTPFuture`
        """
        log.debug(u"%s(%s)" % (self.operation.operation_id, op_kwargs))
        warn_for_deprecated_op(self.operation)
        request_params = self.construct_request(**op_kwargs)
        callback = functools.partial(bitjws_response_callback, operation=self)
        return self.operation.swagger_spec.http_client.request(request_params,
                                                               callback)


def bitjws_response_callback(incoming_response, operation):
    """
    So the http_client is finished with its part of processing the response.
    This hands the response over to bravado_core for validation and
    unmarshalling.

    :type incoming_response: :class:`bravado_core.response.IncomingResponse`
    :type operation: :class:`bravado_core.operation.Operation`
    :return: Response spec's return value.
    :raises: HTTPError
        - On 5XX status code, the HTTPError has minimal information.
        - On non-2XX status code with no matching response, the HTTPError
            contains a detailed error message.
        - On non-2XX status code with a matching response, the HTTPError
            contains the return value.
    """
    raise_on_unexpected(incoming_response)

    try:
        swagger_return_value = unmarshal_response(incoming_response, operation)
    except MatchingResponseNotFound as e:
        six.reraise(
            HTTPError,
            HTTPError(response=incoming_response, message=str(e)),
            sys.exc_info()[2])

    raise_on_expected(incoming_response, swagger_return_value)
    return swagger_return_value
