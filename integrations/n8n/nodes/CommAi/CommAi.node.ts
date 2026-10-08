import type {
  IDataObject,
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { commAiRequest } from '../shared';

const conv = { displayName: 'Conversation ID', name: 'conversationId', type: 'string' as const, default: '', required: true };

export class CommAi implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Jibsy by ExaCarib',
    name: 'commAi',
    icon: 'file:commai.svg',
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description: 'Contacts, conversations, notes and approved actions in Jibsy',
    defaults: { name: 'Jibsy' },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [{ name: 'commAiApi', required: true }],
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        default: 'sendMessage',
        options: [
          { name: 'Create Contact', value: 'createContact' },
          { name: 'Find Contacts', value: 'findContacts' },
          { name: 'Start Conversation', value: 'startConversation' },
          { name: 'Send Message', value: 'sendMessage' },
          { name: 'Add Internal Note', value: 'addNote' },
          { name: 'Propose Action', value: 'proposeAction', description: 'Sensitive actions wait for approval in Jibsy' },
        ],
      },
      { ...conv, displayOptions: { show: { operation: ['sendMessage', 'addNote'] } } },
      {
        displayName: 'Text',
        name: 'body',
        type: 'string',
        typeOptions: { rows: 4 },
        default: '',
        displayOptions: { show: { operation: ['sendMessage', 'addNote', 'startConversation'] } },
      },
      {
        displayName: 'Template',
        name: 'template',
        type: 'string',
        default: '',
        description: 'Needed on WhatsApp outside the 24-hour window',
        displayOptions: { show: { operation: ['sendMessage'] } },
      },
      {
        displayName: 'Channel',
        name: 'channel',
        type: 'options',
        default: 'email',
        options: [
          { name: 'Email', value: 'email' },
          { name: 'SMS', value: 'sms' },
          { name: 'WhatsApp', value: 'whatsapp' },
          { name: 'Web Chat', value: 'webchat' },
        ],
        displayOptions: { show: { operation: ['startConversation'] } },
      },
      {
        displayName: 'Address',
        name: 'address',
        type: 'string',
        default: '',
        required: true,
        description: 'Email address or phone number',
        displayOptions: { show: { operation: ['startConversation'] } },
      },
      {
        displayName: 'Fields',
        name: 'fields',
        type: 'collection',
        default: {},
        displayOptions: { show: { operation: ['createContact', 'startConversation'] } },
        options: [
          { displayName: 'Name', name: 'name', type: 'string', default: '' },
          { displayName: 'Email', name: 'email', type: 'string', default: '' },
          { displayName: 'Phone', name: 'phone', type: 'string', default: '' },
          { displayName: 'Subject', name: 'subject', type: 'string', default: '' },
          { displayName: 'Your Reference', name: 'external_id', type: 'string', default: '' },
        ],
      },
      {
        displayName: 'Search',
        name: 'q',
        type: 'string',
        default: '',
        required: true,
        displayOptions: { show: { operation: ['findContacts'] } },
      },
      {
        displayName: 'App',
        name: 'app',
        type: 'string',
        default: '',
        required: true,
        displayOptions: { show: { operation: ['proposeAction'] } },
      },
      {
        displayName: 'Action',
        name: 'action',
        type: 'string',
        default: '',
        required: true,
        displayOptions: { show: { operation: ['proposeAction'] } },
      },
      {
        displayName: 'Inputs (JSON)',
        name: 'inputs',
        type: 'json',
        default: '{}',
        displayOptions: { show: { operation: ['proposeAction'] } },
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const out: INodeExecutionData[] = [];
    const items = this.getInputData();
    for (let i = 0; i < items.length; i++) {
      const op = this.getNodeParameter('operation', i) as string;
      const p = (name: string, d: unknown = '') => this.getNodeParameter(name, i, d);
      let res: IDataObject | IDataObject[];
      if (op === 'createContact') {
        const f = p('fields', {}) as IDataObject;
        res = await commAiRequest(this, 'POST', '/contacts', {
          name: f.name,
          email: f.email,
          phone: f.phone,
          external_ref: f.external_id,
        });
      } else if (op === 'findContacts') {
        res = ((await commAiRequest(this, 'GET', '/contacts', undefined, { q: p('q'), limit: 10 })).items ||
          []) as IDataObject[];
      } else if (op === 'startConversation') {
        const f = p('fields', {}) as IDataObject;
        res = await commAiRequest(this, 'POST', '/conversations', {
          channel: p('channel'),
          address: p('address'),
          body: p('body'),
          name: f.name,
          subject: f.subject,
          external_id: f.external_id,
        });
      } else if (op === 'sendMessage') {
        const id = encodeURIComponent(p('conversationId') as string);
        res = await commAiRequest(this, 'POST', `/conversations/${id}/messages`, {
          body: p('body'),
          template: p('template') || undefined,
        });
      } else if (op === 'addNote') {
        const id = encodeURIComponent(p('conversationId') as string);
        res = await commAiRequest(this, 'POST', `/conversations/${id}/notes`, { body: p('body') });
      } else {
        const raw = p('inputs', '{}');
        res = await commAiRequest(this, 'POST', '/actions', {
          app: p('app'),
          action: p('action'),
          inputs: typeof raw === 'string' ? JSON.parse(raw) : raw,
        });
      }
      for (const r of Array.isArray(res) ? res : [res]) out.push({ json: r, pairedItem: { item: i } });
    }
    return [out];
  }
}
