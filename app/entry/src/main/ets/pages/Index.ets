import { BinRunner } from '../common/BinRunner';

@Entry
@Component
struct Index {
  @StorageLink('output') output: string =
    '等待指令…\n\nhdc 触发方式：\nhdc shell aa start -b com.example.binrunner -a EntryAbility --es cmd "hello foo bar"\n\n查看输出：\nhdc shell hilog | grep BinRunner';
  @State cmdInput: string = '';

  build() {
    Column({ space: 12 }) {
      Text('BinRunner Demo')
        .fontSize(24)
        .fontWeight(FontWeight.Bold)

      Text(`binDir: ${BinRunner.binDir}`)
        .fontSize(12)
        .fontColor(Color.Gray)
        .width('100%')

      Row({ space: 8 }) {
        TextInput({ placeholder: '例如: hello foo bar', text: this.cmdInput })
          .onChange((value: string) => {
            this.cmdInput = value;
          })
          .layoutWeight(1)
        Button('运行')
          .onClick(() => {
            AppStorage.setOrCreate('output', BinRunner.run(this.cmdInput));
          })
      }
      .width('100%')

      Scroll() {
        Text(this.output)
          .fontSize(13)
          .fontFamily('monospace')
          .width('100%')
          .textAlign(TextAlign.Start)
      }
      .layoutWeight(1)
      .width('100%')
      .backgroundColor('#F5F5F5')
      .borderRadius(8)
      .padding(8)
    }
    .width('100%')
    .height('100%')
    .padding(16)
  }
}
