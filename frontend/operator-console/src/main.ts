import { createApp } from "vue";

import App from "./App.vue";
import { createConsoleRouter } from "./router";
import "./styles/index.css";

const application = createApp(App);
application.use(createConsoleRouter());
application.mount("#app");
