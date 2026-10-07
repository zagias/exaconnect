const { version } = require('./package.json');
const { version: platformVersion } = require('zapier-platform-core');
const authentication = require('./authentication');

const addKey = (request, z, bundle) => {
  if (bundle.authData.api_key) request.headers.Authorization = `Bearer ${bundle.authData.api_key}`;
  return request;
};

const all = (mods) => Object.fromEntries(mods.map((m) => [m.key, m]));

module.exports = {
  version,
  platformVersion,
  authentication,
  beforeRequest: [addKey],
  triggers: all([require('./triggers/new_event'), require('./triggers/event_types')]),
  creates: all([
    require('./creates/create_contact'),
    require('./creates/start_conversation'),
    require('./creates/send_message'),
    require('./creates/add_note'),
    require('./creates/propose_action'),
  ]),
  searches: all([require('./searches/find_contact')]),
};
