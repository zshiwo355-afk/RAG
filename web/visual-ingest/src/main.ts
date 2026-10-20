import { createApp } from 'vue'
import {
  ElAlert, ElButton, ElCard, ElCol, ElCollapse, ElCollapseItem, ElDescriptions,
  ElDescriptionsItem, ElDialog, ElDivider, ElDrawer, ElEmpty, ElForm, ElFormItem,
  ElInput, ElInputNumber, ElOption, ElPagination, ElRadioButton, ElRadioGroup,
  ElRow, ElSelect, ElStep, ElSteps, ElSwitch, ElTabPane, ElTable, ElTableColumn,
  ElTabs, ElTag, ElUpload
} from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/card/style/css'
import 'element-plus/es/components/col/style/css'
import 'element-plus/es/components/collapse/style/css'
import 'element-plus/es/components/collapse-item/style/css'
import 'element-plus/es/components/descriptions/style/css'
import 'element-plus/es/components/descriptions-item/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/divider/style/css'
import 'element-plus/es/components/drawer/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/form/style/css'
import 'element-plus/es/components/form-item/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/input-number/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/pagination/style/css'
import 'element-plus/es/components/radio-button/style/css'
import 'element-plus/es/components/radio-group/style/css'
import 'element-plus/es/components/row/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/step/style/css'
import 'element-plus/es/components/steps/style/css'
import 'element-plus/es/components/switch/style/css'
import 'element-plus/es/components/table/style/css'
import 'element-plus/es/components/table-column/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tab-pane/style/css'
import 'element-plus/es/components/tag/style/css'
import 'element-plus/es/components/upload/style/css'
import './styles.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
for (const component of [ElAlert, ElButton, ElCard, ElCol, ElCollapse, ElCollapseItem,
  ElDescriptions, ElDescriptionsItem, ElDialog, ElDivider, ElDrawer, ElEmpty, ElForm,
  ElFormItem, ElInput, ElInputNumber, ElOption, ElPagination, ElRadioButton, ElRadioGroup,
  ElRow, ElSelect, ElStep, ElSteps, ElSwitch, ElTabPane, ElTable, ElTableColumn,
  ElTabs, ElTag, ElUpload]) app.use(component)
app.use(router).mount('#app')
