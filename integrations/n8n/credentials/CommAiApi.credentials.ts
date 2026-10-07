import type { IAuthenticateGeneric, ICredentialTestRequest, ICredentialType, INodeProperties } from 'n8n-workflow';

export class CommAiApi implements ICredentialType {
  name = 'commAiApi';
  displayName = 'ExaCarib Connect CommAI API';
  properties: INodeProperties[] = [
    {
      displayName: 'Connect address',
      name: 'baseUrl',
      type: 'string',
      default: '',
      placeholder: 'https://connect.example.com',
      required: true,
    },
    {
      displayName: 'API key',
      name: 'apiKey',
      type: 'string',
      typeOptions: { password: true },
      default: '',
      description: 'A CommAI API key from the portal (Admin, API keys). It starts with exa_.',
      required: true,
    },
  ];

  authenticate: IAuthenticateGeneric = {
    type: 'generic',
    properties: { headers: { Authorization: '=Bearer {{$credentials.apiKey}}' } },
  };

  test: ICredentialTestRequest = {
    request: { baseURL: '={{$credentials.baseUrl.replace(/\\/+$/, "")}}', url: '/api/v1/auth/me', method: 'GET' },
  };
}
