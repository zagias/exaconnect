import type {
  IDataObject,
  IHookFunctions,
  ILoadOptionsFunctions,
  INodePropertyOptions,
  INodeType,
  INodeTypeDescription,
  IWebhookFunctions,
  IWebhookResponseData,
} from 'n8n-workflow';
import { commAiRequest, verify } from '../shared';

// Activating a workflow adds a Jibsy webhook endpoint for the chosen events
// and keeps its signing secret in the node's static data; deactivating deletes
// it. Each delivery is checked against that secret before the workflow runs.
export class CommAiTrigger implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Jibsy by ExaCarib Trigger',
    name: 'commAiTrigger',
    icon: 'file:commai.svg',
    group: ['trigger'],
    version: 1,
    description: 'Starts a workflow when something happens in Jibsy',
    defaults: { name: 'Jibsy Trigger' },
    inputs: [],
    outputs: ['main'],
    credentials: [{ name: 'commAiApi', required: true }],
    webhooks: [{ name: 'default', httpMethod: 'POST', responseMode: 'onReceived', path: 'webhook' }],
    properties: [
      {
        displayName: 'Events',
        name: 'events',
        type: 'multiOptions',
        typeOptions: { loadOptionsMethod: 'eventTypes' },
        default: [],
        required: true,
      },
    ],
  };

  methods = {
    loadOptions: {
      async eventTypes(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
        const types = (await commAiRequest(this, 'GET', '/event-types')) as string[];
        return types.map((t) => ({ name: t, value: t }));
      },
    },
  };

  webhookMethods = {
    default: {
      async checkExists(this: IHookFunctions): Promise<boolean> {
        const data = this.getWorkflowStaticData('node');
        if (!data.endpointId) return false;
        const all = (await commAiRequest(this, 'GET', '/webhooks')) as IDataObject[];
        return all.some((e) => e.id === data.endpointId);
      },
      async create(this: IHookFunctions): Promise<boolean> {
        const res = await commAiRequest(this, 'POST', '/webhooks', {
          url: this.getNodeWebhookUrl('default'),
          description: 'n8n',
          events: this.getNodeParameter('events') as string[],
        });
        const data = this.getWorkflowStaticData('node');
        data.endpointId = res.id;
        data.secret = res.secret;
        return true;
      },
      async delete(this: IHookFunctions): Promise<boolean> {
        const data = this.getWorkflowStaticData('node');
        if (data.endpointId) {
          try {
            await commAiRequest(this, 'DELETE', `/webhooks/${data.endpointId}`);
          } catch {
            return false;
          }
        }
        delete data.endpointId;
        delete data.secret;
        return true;
      },
    },
  };

  async webhook(this: IWebhookFunctions): Promise<IWebhookResponseData> {
    const req = this.getRequestObject() as unknown as { rawBody?: Buffer };
    const secret = String(this.getWorkflowStaticData('node').secret || '');
    const raw = req.rawBody ? req.rawBody.toString('utf8') : JSON.stringify(this.getBodyData());
    if (!verify(secret, this.getHeaderData() as IDataObject, raw)) {
      this.getResponseObject().status(401).send('Not signed by Jibsy');
      return { noWebhookResponse: true };
    }
    return { workflowData: [this.helpers.returnJsonArray(this.getBodyData())] };
  }
}
